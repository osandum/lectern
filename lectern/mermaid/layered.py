"""Layered graph layout, shared by every box-and-arrow diagram type.

A cut-down Sugiyama: rank the nodes into layers along the diagram's
direction, order each layer to unpick crossings, then place them across
the layer. That's the shape of the algorithm dagre (what mermaid itself
uses) implements, minus the parts that need a solver -- ordering is
barycentre sweeps rather than an optimal ordering, and cross-axis
placement is priority-based rather than a network simplex. The difference
shows as slightly wider spacing and the odd avoidable crossing on dense
graphs, not as unreadable output.

Callers supply their own node and edge objects; this module only requires
that a node carries `key`, `w`, `h` (already sized) and accepts `x`, `y`,
`rank`, `order`, and that an edge carries `src` and `dst` keys. What the
boxes look like -- a flowchart's shapes, a class diagram's compartments --
is the caller's business entirely, and is why this file draws nothing.
"""

RANK_GAP = 46.0        # between one layer and the next
NODE_GAP = 26.0        # between siblings within a layer
VIRTUAL_W = 14.0       # cross-axis room reserved for an edge passing a layer

VERTICAL_DIRECTIONS = ("TD", "BT")


class Virtual:
    """A placeholder node an edge is routed through when it spans more
    than one rank. Without them a long edge cuts straight across whatever
    happens to sit in the ranks between its ends."""
    __slots__ = ("key", "w", "h", "x", "y", "rank", "order")

    def __init__(self, key, rank):
        self.key = key
        self.w = self.h = VIRTUAL_W
        self.x = self.y = 0.0
        self.rank = rank
        self.order = 0

    @property
    def virtual(self):
        return True


def rank_nodes(nodes, edges):
    """Longest-path ranking over the edges that aren't back edges.

    Cycles are normal in these diagrams (any retry loop is one), so the
    back edges a DFS finds are left out of the ranking and simply drawn
    against the flow, the way dagre does it. Ranking with them included
    would either not terminate or collapse the loop into one rank.
    """
    succ = {key: [] for key in nodes}
    for edge in edges:
        if edge.src != edge.dst:
            succ[edge.src].append(edge.dst)

    WHITE, GREY, BLACK = 0, 1, 2
    color = {key: WHITE for key in nodes}
    back = set()

    def visit(start):
        # Iterative DFS: a deep chain of nodes would otherwise be a
        # recursion depth away from crashing the viewer.
        stack = [(start, iter(succ[start]))]
        color[start] = GREY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if color[child] == GREY:
                    back.add((node, child))
                elif color[child] == WHITE:
                    color[child] = GREY
                    stack.append((child, iter(succ[child])))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()

    for key in nodes:
        if color[key] == WHITE:
            visit(key)

    forward = [(edge.src, edge.dst) for edge in edges
               if edge.src != edge.dst and (edge.src, edge.dst) not in back]
    rank = {key: 0 for key in nodes}
    # Relaxation rather than a topological sort: the forward set is
    # acyclic, so |V| sweeps are guaranteed to settle it, and this needs
    # no second graph structure to walk.
    for _ in range(len(nodes)):
        changed = False
        for src, dst in forward:
            if rank[dst] < rank[src] + 1:
                rank[dst] = rank[src] + 1
                changed = True
        if not changed:
            break
    for key, node in nodes.items():
        node.rank = rank[key]
    return back


def build_layers(nodes, edges):
    """Ranked layers with virtual nodes spliced into every long edge.

    Returns (cells, layers, routes), where routes maps an edge's index to
    the virtual keys it passes through, source-to-target.
    """
    cells = dict(nodes)
    routes = {}
    counter = 0
    for index, edge in enumerate(edges):
        src, dst = nodes[edge.src], nodes[edge.dst]
        low, high = sorted((src.rank, dst.rank))
        if high - low <= 1:
            routes[index] = []
            continue
        chain = []
        for rank in range(low + 1, high):
            counter += 1
            key = f"\0v{counter}"
            cells[key] = Virtual(key, rank)
            chain.append(key)
        if src.rank > dst.rank:
            chain.reverse()
        routes[index] = chain
    max_rank = max(cell.rank for cell in cells.values())
    layers = [[] for _ in range(max_rank + 1)]
    for cell in cells.values():
        layers[cell.rank].append(cell)
    return cells, layers, routes


def neighbour_map(edges, cells, routes):
    """Adjacency over the *expanded* graph (virtual nodes included), which
    is what ordering and placement both work on."""
    up = {key: [] for key in cells}
    down = {key: [] for key in cells}
    for index, edge in enumerate(edges):
        path = [edge.src] + routes[index] + [edge.dst]
        for a, b in zip(path, path[1:]):
            if cells[a].rank == cells[b].rank:
                continue
            lower, upper = (a, b) if cells[a].rank < cells[b].rank else (b, a)
            down[lower].append(upper)
            up[upper].append(lower)
    return up, down


def order_layers(layers, up, down, passes=4):
    """Barycentre sweeps: repeatedly reorder each layer by the average
    position of its neighbours in the layer just processed. Two or three
    passes get most of the crossings a diagram-sized graph has; more stops
    paying."""
    cells_by_key = {cell.key: cell for layer in layers for cell in layer}
    for cell_list in layers:
        for position, cell in enumerate(cell_list):
            cell.order = position

    def sweep(neighbours, ordered_layers):
        for cell_list in ordered_layers:
            def barycentre(cell):
                positions = [cells_by_key[k].order for k in neighbours[cell.key]]
                # A cell with no neighbours in the reference layer keeps
                # its current position rather than being swept to the
                # front, which is what a 0 barycentre would do.
                return sum(positions) / len(positions) if positions else cell.order
            cell_list.sort(key=barycentre)
            for position, cell in enumerate(cell_list):
                cell.order = position

    for index in range(passes):
        if index % 2 == 0:
            sweep(up, layers[1:])
        else:
            sweep(down, layers[-2::-1])


def _cross_size(cell, vertical):
    return cell.w if vertical else cell.h


def _rank_size(cell, vertical):
    return cell.h if vertical else cell.w


def place(layers, up, down, vertical, rank_gaps):
    """Assign every cell a centre on both axes.

    Rank axis: layers are stacked by their tallest (widest) member, the
    gap after layer `i` being `rank_gaps[i]`.
    Cross axis: pack each layer, then pull cells toward the average of
    their neighbours in adjacent layers and re-separate. Sweeping in both
    directions keeps a node with only successors (a source) as
    well-aligned as one with only predecessors.
    """
    cells_by_key = {cell.key: cell for layer in layers for cell in layer}
    positions = {}

    rank_pos = 0.0
    for layer, gap in zip(layers, rank_gaps):
        extent = max((_rank_size(cell, vertical) for cell in layer), default=0.0)
        for cell in layer:
            positions[cell.key] = [0.0, rank_pos + extent / 2]
        rank_pos += extent + gap

    def pack(layer):
        cursor = 0.0
        for cell in layer:
            half = _cross_size(cell, vertical) / 2
            positions[cell.key][0] = max(positions[cell.key][0], cursor + half)
            cursor = positions[cell.key][0] + half + NODE_GAP

    def separate(layer):
        """Push apart in both directions so a pulled cell never overlaps
        its neighbour, and the pull isn't systematically biased toward
        whichever end the pass started from."""
        for index in range(1, len(layer)):
            prev, cell = layer[index - 1], layer[index]
            minimum = (positions[prev.key][0] + _cross_size(prev, vertical) / 2
                       + NODE_GAP + _cross_size(cell, vertical) / 2)
            positions[cell.key][0] = max(positions[cell.key][0], minimum)
        for index in range(len(layer) - 2, -1, -1):
            cell, following = layer[index], layer[index + 1]
            maximum = (positions[following.key][0] - _cross_size(following, vertical) / 2
                       - NODE_GAP - _cross_size(cell, vertical) / 2)
            positions[cell.key][0] = min(positions[cell.key][0], maximum)

    for layer in layers:
        pack(layer)

    for iteration in range(4):
        neighbours = up if iteration % 2 == 0 else down
        ordered = layers[1:] if iteration % 2 == 0 else layers[-2::-1]
        for layer in ordered:
            for cell in layer:
                keys = neighbours[cell.key]
                if keys:
                    positions[cell.key][0] = sum(
                        positions[cells_by_key[k].key][0] for k in keys) / len(keys)
            layer.sort(key=lambda cell: positions[cell.key][0])
            separate(layer)

    # Nothing above pins the diagram to the origin; shift it there so the
    # scene's own box starts at (0, 0).
    lowest = min(positions[cell.key][0] - _cross_size(cell, vertical) / 2
                 for layer in layers for cell in layer)
    for layer in layers:
        for cell in layer:
            cross, rank = positions[cell.key]
            cross -= lowest
            if vertical:
                cell.x, cell.y = cross, rank
            else:
                cell.x, cell.y = rank, cross


def mirror(cells, direction, width, height):
    """BT and RL are TD and LR drawn backwards along the rank axis."""
    for cell in cells.values():
        if direction == "BT":
            cell.y = height - cell.y
        elif direction == "RL":
            cell.x = width - cell.x


def layout(nodes, edges, direction, rank_gap=RANK_GAP, edge_room=None):
    """Rank, order and place `nodes`. Returns (cells, routes, width,
    height); the nodes themselves come back with x/y/rank filled in.

    `rank_gap` is a caller's choice because it is really "how much room do
    this diagram's edges need": a flowchart's carry a short label at most,
    while a class or ER relation carries a label *and* a cardinality at
    each end, which need clear line either side of the label's plate.

    `edge_room` maps an edge's index to the length along the rank axis it
    needs between its two ends -- a label plate plus line either side --
    and widens just the gap that edge crosses. A uniform gap can't be
    sized for the longest label without spreading every other layer, and
    one sized for the typical label lets a long plate cover the gap
    entirely, its ends disappearing under the nodes either side. Several
    labelled edges sharing an end widen it by all of their rooms together.
    """
    vertical = direction in VERTICAL_DIRECTIONS
    rank_nodes(nodes, edges)
    cells, layers, routes = build_layers(nodes, edges)
    up, down = neighbour_map(edges, cells, routes)
    order_layers(layers, up, down)
    rank_gaps = [rank_gap] * len(layers)
    # Labelled edges crossing a gap from (or to) the same node fan out of
    # one point, so their labels all want the same spot beside it. The gap
    # gets room for them one after another along the rank axis, which is
    # what lets place_labels slide them apart; a lone label's bundle is
    # just its own room, so it reserves exactly what it did before.
    bundles = {}
    for index, room in (edge_room or {}).items():
        edge = edges[index]
        # Only an edge between adjacent layers is confined to one gap. A
        # longer one is routed through virtual nodes, and the label goes
        # on its longest segment, which spans a whole layer as well as a
        # gap -- already room the gap alone doesn't have to provide.
        if not routes[index] and edge.src != edge.dst:
            low = min(nodes[edge.src].rank, nodes[edge.dst].rank)
            for end in (edge.src, edge.dst):
                bundles[(low, end)] = bundles.get((low, end), 0.0) + room
    for (low, _end), room in bundles.items():
        rank_gaps[low] = max(rank_gaps[low], room)
    place(layers, up, down, vertical, rank_gaps)
    width = max((cell.x + cell.w / 2 for cell in cells.values()), default=0.0)
    height = max((cell.y + cell.h / 2 for cell in cells.values()), default=0.0)
    mirror(cells, direction, width, height)
    return cells, routes, width, height


# -- edge geometry ---------------------------------------------------------

def boundary_point(cell, toward, shape=None):
    """Where the line from `cell`'s centre to `toward` leaves the cell.

    Everything is clipped against the bounding box (circles against their
    ellipse), which is close enough for the diamond and hexagon that the
    couple of pixels of overshoot land under the arrowhead anyway.
    """
    cx, cy = cell.x, cell.y
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    if (shape or getattr(cell, "shape", None)) == "circle":
        radius_x, radius_y = cell.w / 2, cell.h / 2
        norm = ((dx / radius_x) ** 2 + (dy / radius_y) ** 2) ** 0.5
        return cx + dx / norm, cy + dy / norm
    half_w, half_h = cell.w / 2, cell.h / 2
    scale = min(half_w / abs(dx) if dx else float("inf"),
                half_h / abs(dy) if dy else float("inf"))
    return cx + dx * scale, cy + dy * scale


def edge_points(cells, routes, index, edge):
    src, dst = cells[edge.src], cells[edge.dst]
    middle = [(cells[key].x, cells[key].y) for key in routes[index]]
    first_target = middle[0] if middle else (dst.x, dst.y)
    last_source = middle[-1] if middle else (src.x, src.y)
    start = boundary_point(src, first_target)
    end = boundary_point(dst, last_source)
    return [start] + middle + [end]


def self_loop_points(cell):
    """A short arc out of the node's right side and back into its top --
    a self-edge has nowhere else to go, and mermaid draws it the same
    way."""
    right = cell.x + cell.w / 2
    top = cell.y - cell.h / 2
    reach = max(18.0, cell.h * 0.5)
    return [
        (right, cell.y - cell.h * 0.15),
        (right + reach, cell.y - cell.h * 0.15),
        (right + reach, top - reach * 0.6),
        (cell.x + cell.w * 0.2, top - reach * 0.6),
        (cell.x + cell.w * 0.2, top),
    ]


def label_position(points):
    """Midpoint of the edge's longest segment -- the place with the most
    room for a plate, rather than the geometric middle, which on an
    L-shaped route lands on the corner."""
    best, best_length = None, -1.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if length > best_length:
            best, best_length = ((x1 + x2) / 2, (y1 + y2) / 2), length
    return best


# Clear space kept around a label plate when checking it against others.
LABEL_CLEARANCE = 2.0
# Where along an edge (as a fraction of its length) a label may move to
# when its first choice collides, nearest the middle first. Not past 0.2
# from either end, where the arrowheads and cardinalities sit.
_LABEL_FRACTIONS = (0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.2, 0.8)


def _point_along(points, fraction):
    lengths = [((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
               for (x1, y1), (x2, y2) in zip(points, points[1:])]
    remaining = sum(lengths) * fraction
    for ((x1, y1), (x2, y2)), length in zip(zip(points, points[1:]), lengths):
        if remaining <= length and length > 0:
            t = remaining / length
            return x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        remaining -= length
    return points[-1]


def _overlap_area(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0.0


def place_labels(requests, obstacles=()):
    """Centres for edge labels that don't sit on top of each other.

    `requests` is a list of (edge points, plate width, plate height), and
    `obstacles` the (x, y, w, h) boxes no plate should cover -- the nodes,
    and anything already placed such as cardinalities. Labels are placed
    in order. Each keeps `label_position` when that is clear, so a diagram
    with no collisions comes out exactly as it did before this pass
    existed; otherwise it slides along its own edge, nearest the middle
    first, to the first position that is clear. If none is, it takes the
    one that overlaps least rather than leaving the pile-up as it was.

    Two labels on edges that leave one node for the same layer are the
    usual collision: their midpoints are a few pixels apart, and every
    label on a short edge has nowhere else to be but its middle.
    """
    taken = list(obstacles)
    centres = []
    for points, w, h in requests:
        candidates = [label_position(points)]
        candidates += [_point_along(points, f) for f in _LABEL_FRACTIONS]
        best, best_cost = None, None
        for cx, cy in candidates:
            plate = (cx - w / 2 - LABEL_CLEARANCE, cy - h / 2 - LABEL_CLEARANCE,
                     w + 2 * LABEL_CLEARANCE, h + 2 * LABEL_CLEARANCE)
            cost = sum(_overlap_area(plate, box) for box in taken)
            if best_cost is None or cost < best_cost:
                best, best_cost = (cx, cy, plate), cost
            if cost == 0:
                break
        cx, cy, plate = best
        taken.append(plate)
        centres.append((cx, cy))
    return centres
