"""Map an orthogonal source through printed controls into the existing CAD working frame."""
from collections import defaultdict
from copy import deepcopy
from math import copysign, dist, hypot, isfinite
from statistics import median

from vendor.house_plan_designer.axismap import Axis, chain
import wall_thickness

EPS = 1e-6  # arithmetic equality in WCS mm
AXES = ('x', 'y')
KINDS = {'wall_center', 'wall_face', 'opening_end', 'column_edge', 'datum'}


def number(value, label):
    if type(value) not in (float, int) or not isfinite(value):
        raise ValueError(f'{label}: expected a finite number')
    return float(value)


def point_xy(values, label):
    if len(values) != 2:
        raise ValueError(f'{label}: expected two coordinates')
    return [number(value, label) for value in values]


def linear(matrix, point):
    a, b, d, e, tx, ty = matrix
    x, y = point
    return [a*x + b*y + tx, d*x + e*y + ty]


def invert(matrix, point):
    a, b, d, e, tx, ty = matrix
    x, y = point[0] - tx, point[1] - ty
    det = a*e - b*d
    return [(e*x - b*y)/det, (a*y - d*x)/det]


class GridProjection:
    """Registration accepts the original six-element affine, with optional piecewise axes."""
    def __init__(self, registration):
        self.matrix = tuple(number(value,'source_to_mm') for value in registration['source_to_mm'])
        if len(self.matrix)!=6 or abs(self.matrix[0]*self.matrix[3]-self.matrix[1]*self.matrix[2])<1e-12:
            raise ValueError('Expected six invertible affine coefficients')
        self.axes = [None,None]
        if 'grid_registration' not in registration:
            return
        knots = registration['grid_registration']['axes']
        if any(abs(self.matrix[i])>1e-10 for i in (1,2)):
            raise ValueError('Piecewise controls require rectified orthogonal source axes')
        for i,name in enumerate(AXES):
            pairs = [point_xy(row,name) for row in knots[name]]
            if not pairs:
                continue
            sign = 1 if i==0 else -1
            if any(round(a[0],2)>=round(b[0],2) or sign*(b[1]-a[1])<=0 for a,b in zip(pairs,pairs[1:])):
                raise ValueError(f'{name}: saved grid stations must be strictly monotone and distinct')
            self.axes[i] = Axis(pairs)

    def world(self, original_px):
        preliminary = linear(self.matrix, original_px)
        return [axis(p, snap=0) if axis else value
                for axis, p, value in zip(self.axes, original_px, preliminary)]

    def source(self, world_mm):
        result = invert(self.matrix, world_mm)
        for i, axis in enumerate(self.axes):
            if axis is None:
                continue
            value = world_mm[i]
            sign = 1 if axis.scale > 0 else -1
            if sign*(value-axis.mm[0]) <= 0:
                result[i] = axis.px[0] + (value-axis.mm[0])/axis.scale
            elif sign*(value-axis.mm[-1]) >= 0:
                result[i] = axis.px[-1] + (value-axis.mm[-1])/axis.scale
            else:
                j = next(j for j in range(1, len(axis.mm)) if sign*(axis.mm[j]-value) >= 0)
                ratio = (value-axis.mm[j-1])/(axis.mm[j]-axis.mm[j-1])
                result[i] = axis.px[j-1] + ratio*(axis.px[j]-axis.px[j-1])
        return result

    def working(self, original_px):
        return invert(self.matrix, self.world(original_px))

    def signed_distance(self, i, raw_a, raw_b, fixed=0):
        """Millimetres along one source axis; increasing pixels always have positive sign."""
        if i not in (0,1):
            raise ValueError('Expected source axis index 0 or 1')
        a, b = ([t,fixed] if i==0 else [fixed,t] for t in (raw_a,raw_b))
        return copysign(dist(self.world(a),self.world(b)),raw_b-raw_a)

    def _rates(self,i):
        if i not in (0,1):
            raise ValueError('Expected source axis index 0 or 1')
        axis = self.axes[i]
        if axis is None:
            return [hypot(self.matrix[i],self.matrix[2+i])]
        return [abs(axis.scale), *(abs((n-m)/(b-a)) for a,b,m,n in
                zip(axis.px, axis.px[1:], axis.mm, axis.mm[1:]))]

    def min_mm_per_px(self,i):
        return min(self._rates(i))

    def max_mm_per_px(self,i):
        return max(self._rates(i))

    def _path(self, points, inverse):
        transform = self.source if inverse else self.world
        if not len(points):
            return []
        result = [transform(points[0])]
        for a, b in zip(points, points[1:]):
            fractions = {1.0}
            for i, axis in enumerate(self.axes):
                if axis is None or a[i] == b[i]:
                    continue
                for tick in (axis.mm if inverse else axis.px):
                    if min(a[i], b[i]) < tick < max(a[i], b[i]):
                        fractions.add((tick-a[i])/(b[i]-a[i]))
            for t in sorted(fractions):
                p = [u + t*(v-u) for u, v in zip(a, b)]
                result.append(transform(p))
        return result

    def world_path(self, original_points):
        return self._path(original_points, False)

    def source_path(self, world_points):
        return self._path(world_points, True)


def solve_grid(scene):
    spec = scene['dimension_grid']
    mode = spec['mode']
    printed = scene.get('dimension_chains', [])
    if mode not in ('chains', 'overall', 'unmarked') or bool(printed) != (mode != 'unmarked'):
        raise ValueError('Use chains or overall for printed spans, unmarked for a scale reference alone')
    controls = spec.get('controls', {})
    active = {}
    for ident, row in controls.items():
        if not ident or row['axis'] not in AXES or row['kind'] not in KINDS or not row.get('evidence'):
            raise ValueError(f'{ident}: identify axis, observed control kind and source evidence')
        if row['kind'] != 'datum' and not row.get('bindings'):
            raise ValueError(f'{ident}: bind the observed feature to its CAD geometry, or register an independent datum')
        active[ident] = number(row['px'], ident)
    chains = printed
    if mode == 'unmarked':
        ref = spec['reference']
        if ref['kind'] not in ('known_length', 'assumed_wall_thickness') or not ref.get('evidence'):
            raise ValueError('State the known reference or provisional wall thickness and its evidence')
        chains = [dict(ref, id='scale-reference', values_mm=[ref['value_mm']])]
        if ref['kind']=='assumed_wall_thickness':
            faces = []
            for q in ref['controls']:
                c = controls.get(q,{})
                faces.append({(b['wall'],b['at']) for b in c.get('bindings',[]) if 'wall' in b
                              and c.get('kind')=='wall_face' and b['at'] in ('min_face','max_face')})
            if len(faces)!=2 or not any((wall,'max_face' if side=='min_face' else 'min_face') in faces[1]
                                        for wall,side in faces[0]):
                raise ValueError('Scale from provisional wall thickness requires opposite faces of the same wall')
    origin_px = point_xy(spec.get('origin_px', [0, 0]), 'origin_px')
    origin_mm = point_xy(spec.get('origin_mm', [0, 0]), 'origin_mm')
    matrix = scene.get('source_to_mm')
    if matrix is not None:
        if len(matrix) != 6:
            raise ValueError('source_to_mm requires six affine coefficients')
        matrix = [number(v, 'source_to_mm') for v in matrix]
        if matrix[0] <= 0 or matrix[3] >= 0 or max(abs(matrix[1]), abs(matrix[2])) > 1e-10:
            raise ValueError('Rectify source axes before using a dimension grid')
        if 'origin_px' not in spec and 'origin_mm' not in spec:
            origin_mm = linear(matrix, origin_px)
    max_error = number(spec.get('tick_uncertainty_px', 0), 'tick_uncertainty_px')
    if max_error < 0:
        raise ValueError('tick_uncertainty_px must be nonnegative')
    graph = defaultdict(list)
    absolute = {}
    slopes = defaultdict(list)
    readings = []
    chain_ids = set()
    for row in chains:
        name, axis = row['id'], row['axis']
        if not name or name in chain_ids or axis not in AXES:
            raise ValueError(f'{name}: duplicate ID or invalid axis')
        chain_ids.add(name)
        ids = row['controls']
        if any(q not in controls or controls[q]['axis'] != axis for q in ids):
            raise ValueError(f'{name}: unknown control or wrong axis')
        raw_ticks = row.get('ticks_px', [active[q] for q in ids])
        ticks = [number(p, name) for p in raw_ticks]
        lengths = [number(v, name) for v in row['values_mm']]
        if not lengths or len(ids) != len(lengths)+1 or len(ticks) != len(ids) or len(set(ids)) != len(ids):
            raise ValueError(f'{name}: identify each tick and each continuous printed segment')
        if mode in ('overall', 'unmarked') and len(lengths) != 1:
            raise ValueError(f'{name}: overall and scale references each have two endpoints')
        canonical = [active[q] for q in ids]
        sign = 1 if canonical[-1] > canonical[0] else -1
        if any(sign*(b-a) <= 0 for seq in (ticks, canonical) for a, b in zip(seq, seq[1:])) or min(lengths)<=0:
            raise ValueError(f'{name}: controls and ticks must be monotone and spans positive')
        if any(abs(raw-p) > max_error + EPS for raw, p in zip(ticks, canonical)):
            raise ValueError(f'{name}: source tick differs from its named control beyond the stated pixel uncertainty')
        world_sign = sign * (1 if axis == 'x' else -1)
        relative = chain(canonical, [v*world_sign for v in lengths])
        for q, r, (_, a), (_, b) in zip(ids, ids[1:], relative, relative[1:]):
            graph[q].append((r, b-a)); graph[r].append((q, a-b))
        if 'start_mm' in row:
            start = number(row['start_mm'], name)
            if ids[0] in absolute and abs(absolute[ids[0]]-start) > EPS:
                raise ValueError(f'{name}: conflicting WCS start on {ids[0]}')
            absolute[ids[0]] = start
        slopes[axis].append(sum(lengths)/abs(canonical[-1]-canonical[0]))
        readings.append(dict(id=name, axis=axis, controls=ids, ticks_px=ticks, values_mm=lengths,
                             start_mm=row.get('start_mm'), tick_offsets_px=[p-q for p,q in zip(ticks, canonical)]))
    if set(active) != set(graph):
        raise ValueError('Grid controls must belong to a printed chain or the scale reference')
    inferred_scale = median([s for values in slopes.values() for s in values])
    if matrix is None:
        sx = median(slopes.get('x', [inferred_scale]))
        sy = -median(slopes.get('y', [inferred_scale]))
        matrix = [sx, 0, 0, sy, origin_mm[0]-sx*origin_px[0], origin_mm[1]-sy*origin_px[1]]
    elif 'origin_px' in spec or 'origin_mm' in spec:
        matrix = list(matrix)
        matrix[4] = origin_mm[0] - matrix[0]*origin_px[0]
        matrix[5] = origin_mm[1] - matrix[3]*origin_px[1]
    positions = {}
    components = defaultdict(list)
    for row in readings:
        seed = row['controls'][0]
        if seed in positions:
            continue
        relatives = {seed: 0}
        todo = [seed]
        while todo:
            u = todo.pop()
            for v, step in graph[u]:
                new = relatives[u]+step
                if v not in relatives:
                    relatives[v] = new
                    todo.append(v)
                elif abs(relatives[v]-new)>EPS:
                    raise ValueError(f'{u}/{v}: contradictory printed spans in a closed chain')
        axis = row['axis']
        i = AXES.index(axis)
        anchors = [absolute[q]-rel for q, rel in relatives.items() if q in absolute]
        components[axis].append(bool(anchors))
        base = anchors[0] if anchors else matrix[3*i]*active[seed]+matrix[4+i]
        if any(abs(anchor-base)>EPS for anchor in anchors):
            raise ValueError(f'{seed}: conflicting explicit WCS starts')
        positions.update((q, base+val) for q, val in relatives.items())
    for axis, anchored in components.items():
        if len(anchored)>1 and anchored.count(False)>1:
            raise ValueError(f'{axis}: disconnected chains need a shared control or an explicit WCS start')
    knots = {}
    for i, axis in enumerate(AXES):
        per_tick = {}
        for q, mm in positions.items():
            if controls[q]['axis'] != axis:
                continue
            px = round(active[q], 2)  # upstream Axis uses this precision
            if abs(px-active[q])>1e-9:
                raise ValueError(f'{q}: canonical source pixels must use at most two decimal places')
            if px in per_tick and abs(per_tick[px]-mm)>EPS:
                raise ValueError(f'{q}: same source station has two WCS positions')
            per_tick[px] = mm
        pairs = sorted(per_tick.items())
        if any((b[1]-a[1])*(1 if i==0 else -1)<=0 for a,b in zip(pairs,pairs[1:])):
            raise ValueError(f'{axis}: nonmonotonic controls; identify shared stations or review conflicting dimensions')
        knots[axis] = pairs
    intervals = {}
    for axis, pairs in knots.items():
        if not pairs:
            continue
        average = abs(Axis(pairs).scale)
        rows = []
        for (px1,mm1),(px2,mm2) in zip(pairs,pairs[1:]):
            rate = abs((mm2-mm1)/(px2-px1))
            delta = rate/average-1
            rows.append(dict(ticks_px=[px1,px2], millimetres_per_pixel=rate,
                             relative_to_mean=delta, review_scale=px2-px1>15 and abs(delta)>.03))
        intervals[axis] = dict(extrapolated_mm_per_px=average, intervals=rows)
    report = dict(mode=mode, axes=knots, intervals=intervals,
                  controls={q:dict(controls[q], mm=v) for q,v in positions.items()},
                  chains=readings, scale_provenance=spec.get('reference') if mode=='unmarked' else 'printed',
                  unmeasured_axes=[k for k in AXES if not knots[k]],
                  fallback_basis='supplied affine' if 'source_to_mm' in scene else 'equal pixel pitch on unmeasured axis')
    return list(matrix), report


def constrain_geometry(source, work, projection, report, update_witnesses=True):
    """Set exact WCS stations on named straight wall faces, axes and net jambs."""
    by_type = {kind: {row['id']: row for row in work[kind+'s']} for kind in ('wall','opening')}
    original = {kind: {row['id']: row for row in source[kind+'s']} for kind in by_type}
    fixed = {}
    def assign(kind, ident, end, i, value):
        key = kind, ident, end, i
        if key in fixed and abs(fixed[key]-value)>EPS:
            raise ValueError(f'{key}: conflicting geometry controls')
        fixed[key] = value

    def orientation(row):
        a, b = row['a'], row['b']
        if (a[0]==b[0]) == (a[1]==b[1]):
            raise ValueError(f"{row['id']}: expected an orthogonal source segment")
        return 0 if a[1]==b[1] else 1

    for ident, normal, station in wall_thickness.fixed_stations(source, work, projection.matrix):
        for end in ('a', 'b'):
            assign('wall', ident, end, normal, station)

    for q, control in report['controls'].items():
        i = AXES.index(control['axis'])
        mm = control['mm']
        has_feature = control['kind'] == 'datum'
        for bind in control.get('bindings', []):
            kinds = [t for t in by_type if t in bind]
            if len(kinds)!=1:
                raise ValueError(f'{q}: bind one named wall or opening')
            kind = kinds[0]
            ident, at = bind[kind], bind['at']
            row = original[kind][ident]
            tangent = orientation(row)
            if at in ('center','min_face','max_face'):
                if kind!='wall' or i==tangent:
                    raise ValueError(f'{q}: wall-face/axis control must be normal to the wall')
                delta = {'min_face':1, 'max_face':-1, 'center':0}[at]*row['width_mm']/2
                for end in ('a','b'):
                    assign('wall', ident, end, i, mm+delta)
                feature = 'wall_center' if at=='center' else 'wall_face'
                has_feature |= control['kind']==feature or (feature=='wall_face' and control['kind']=='column_edge')
            elif at in ('a','b') and i==tangent:
                assign(kind, ident, at, i, mm)
                has_feature |= (kind=='opening' and control['kind']=='opening_end') or (
                    kind=='wall' and control['kind']=='column_edge' and row.get('cap_'+at+'_mm')==0)
            else:
                raise ValueError(f'{q}: invalid member or orientation for a wall/jamb endpoint')
        if not has_feature:
            raise ValueError(f'{q}: control kind does not match any bound physical feature')

    # Every endpoint at a coincident source wall junction shares its station; hosted
    # jambs share the host normal. Solve these equalities together, including T junctions.
    parents = {}
    def root(key):
        parents.setdefault(key,key)
        while key!=parents[key]:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def join(a,b):
        parents[root(a)] = root(b)

    axes = defaultdict(list)
    for ident, wall in original['wall'].items():
        ti = orientation(wall)
        ni = 1-ti
        join(('wall',ident,'a',ni), ('wall',ident,'b',ni))
        span = sorted(wall[k][ti] for k in ('a','b'))
        axes[ni,wall['a'][ni]].append((ident,span))
    for ident, wall in original['wall'].items():
        ti = orientation(wall)
        for end in ('a','b'):
            pt = wall[end]
            for i in range(2):
                for neighbor,(lo,hi) in axes[i,pt[i]]:
                    if lo<=pt[1-i]<=hi:
                        join(('wall',ident,end,i), ('wall',neighbor,'a',i))
    for ident, op in original['opening'].items():
        ti = orientation(op)
        ni = 1-ti
        for host in op['hosts']:
            wall = original['wall'][host]
            if op['a'][ni]!=wall['a'][ni] or orientation(wall)!=ti:
                raise ValueError(f'{ident}: check source host centreline before using the dimension grid')
            for end in ('a','b'):
                join(('opening',ident,end,ni), ('wall',host,'a',ni))
    group_position = {}
    for key, value in fixed.items():
        group = root(key)
        if group in group_position and abs(group_position[group]-value)>EPS:
            raise ValueError(f'{key}: conflicting bound controls at a connected wall or jamb')
        group_position[group] = value
    for key in parents:
        group = root(key)
        if group not in group_position:
            continue
        kind,ident,end,i = key
        # Coordinates outside this module use the working affine; never quantize prior to DWG output.
        by_type[kind][ident][end][i] = (group_position[group]-projection.matrix[4+i])/projection.matrix[3*i]
    for ident, row in original['wall'].items():
        ni = 1-orientation(row)
        working = by_type['wall'][ident]
        for k, source_p in enumerate(row.get('proof_line',[])):
            if source_p[ni]==row['a'][ni]:
                working['proof_line'][k][ni] = working['a'][ni]
    for row in (work.get('dimension_chains', []) if update_witnesses else []):
        i = AXES.index(row['axis'])
        ni = 1-i
        witnesses = []
        for q in row['controls']:
            ctrl = report['controls'][q]
            raw = [ctrl['px'], row['witness_px']] if i==0 else [row['witness_px'], ctrl['px']]
            p = projection.world(raw)
            p[i] = ctrl['mm']
            candidates = []
            for bind in ctrl.get('bindings', []):
                kind = 'wall' if 'wall' in bind else 'opening'
                entity = by_type[kind][bind[kind]]
                at = bind['at']
                if at in ('a','b'):
                    candidates.append(linear(projection.matrix, entity[at])[ni])
                else:
                    low,high = sorted(linear(projection.matrix,entity[k])[ni] for k in ('a','b'))
                    candidates.append(max(low,min(high,p[ni])))
            if candidates:
                p[ni] = min(candidates,key=lambda n:abs(n-p[ni]))
            witnesses.append(p)
        row['grid_witness_mm'] = witnesses
        raw[ni] = row['line_px']
        row['grid_dim_line_mm'] = projection.world(raw)


def prepare_plan(scene, objects):
    """Project original source observations exactly once; keep original JSON and readback witnesses intact."""
    original_scene = scene
    scene = wall_thickness.prepare(scene)
    if 'dimension_grid' not in scene:
        records = wall_thickness.records(scene)
        if records and any(not r['applied'] for r in records.values()):
            result = deepcopy(scene)
            constrain_geometry(scene, result, GridProjection(result), {'controls': {}}, update_witnesses=False)
            wall_thickness.mark_applied(result)
            if 'printed_wall_tones' in original_scene:
                from apply_wall_tones import base_hash
                tones = original_scene['printed_wall_tones']
                if (tones['base_sha256'] != base_hash(original_scene) or
                        tones['source_sha256'] != original_scene.get('source_sha256')):
                    raise ValueError('Stale original wall-colour evidence; check original scene and image')
                result['printed_wall_tones']['source_base_sha256'] = tones['base_sha256']
                result['printed_wall_tones']['base_sha256'] = base_hash(result)
            return result, objects
        return scene, objects
    if 'grid_registration' in scene:
        raise ValueError('Mixing a prepared registration and original dimension_grid observations is ambiguous')
    matrix, report = solve_grid(scene)
    result, furniture = deepcopy(scene), deepcopy(objects)
    result.pop('dimension_grid')
    result['source_to_mm'] = matrix
    result['grid_registration'] = report
    proj = GridProjection(result)

    def polygon(points):
        if not points:
            return []
        vertices = proj.world_path([*points, points[0]])[:-1]
        return [invert(matrix,p) for p in vertices]

    for wall in result['walls']:
        for k in ('a','b'):
            wall[k] = proj.working(wall[k])
        if 'proof_line' in wall:
            wall['proof_line'] = [proj.working(p) for p in wall['proof_line']]
    for op in result['openings']:
        for k in ('a','b','toward'):
            if k in op:
                op[k] = proj.working(op[k])
        for leaf in op.get('leaves',[]):
            if 'toward' in leaf:
                leaf['toward'] = proj.working(leaf['toward'])
        if 'solid_ends' in op:
            op['solid_ends'] = {k:polygon(v) for k,v in op['solid_ends'].items()}
    for room in result['rooms']:
        if 'free_points' in room:
            room['free_points'] = [proj.working(p) for p in room['free_points']]
        if 'free_polygons' in room:
            room['free_polygons'] = [polygon(pts) for pts in room['free_polygons']]
    constrain_geometry(scene, result, proj, report)
    wall_thickness.mark_applied(result)
    for row in furniture:
        for k in ('back_px','near_px','inside_px','center_px','toward_px','axis_toward_px'):
            if k in row:
                row[k] = proj.working(row[k])
        if 'center_zone_px' in row:
            row['center_zone_px'] = [proj.working(p) for p in row['center_zone_px']]
        if 'outline_px' in row:
            row['outline_px'] = polygon(row['outline_px'])
        if 'facing_px' in row:
            value = row['facing_px']
            row['facing_px'] = [proj.working(p) for p in value] if isinstance(value[0],(list,tuple)) else proj.working(value)
    if 'printed_wall_tones' in scene:
        from apply_wall_tones import base_hash
        source_tones = scene['printed_wall_tones']
        if source_tones['base_sha256']!=base_hash(original_scene) or source_tones['source_sha256']!=scene.get('source_sha256'):
            raise ValueError('Stale original wall-colour evidence; check original scene and image')
        tones = result['printed_wall_tones']
        tones['source_base_sha256'] = source_tones['base_sha256']
        walls = {w['id']: w for w in scene['walls']}
        for span in tones['spans']:
            wall = walls[span['wall']]
            i = int(wall['a'][0]==wall['b'][0])
            span['original_span_px'] = span['span_px']
            span['span_px'] = [proj.working([t,0] if i==0 else [0,t])[i] for t in span['span_px']]
        tones['base_sha256'] = base_hash(result)
    return result, furniture
