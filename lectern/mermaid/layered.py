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

import math

RANK_GAP = 46.0        # between one layer and the next
NODE_GAP = 26.0        # between siblings within a layer
VIRTUAL_W = 14.0       # cross-axis room reserved for an edge passing a layer

VERTICAL_DIRECTIONS = ("TD", "BT")


class Virtual:
    """A placeholder node an edge is routed through when it spans more
    than one rank. Without them a long edge cuts straight across whatever
    happens to sit in the ranks between its ends.

    Once placed, a virtual spans its whole layer along the rank axis (see
    place), and the edge runs straight through it from one side of the
    layer to the other. Bending at a point in the layer's middle instead
    sends the edge diagonally across half of the layer on either side --
    through the boxes beside it, whenever its ends are far apart across.

    `label` is the index of the edge whose label this virtual makes room
    for, if any (see layout)."""
    __slots__ = ("key", "w", "h", "x", "y", "rank", "order", "label", "across")

    def __init__(self, key, rank):
        self.key = key
        self.w = self.h = VIRTUAL_W
        self.x = self.y = 0.0
        self.rank = rank
        self.order = 0
        self.label = None
        self.across = True     # layers stacked top to bottom (see place)

    @property
    def virtual(self):
        return True


def rank_nodes(nodes, edges):
    """Longest-path ranking over the edges that aren't back edges, then
    tightened.

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
    # Longest-path ranking puts every node as high as it can go, which
    # leaves a source (or any node with more edges out than in) hanging
    # far above what it points at, its edges spanning several ranks and
    # crossing everything in between. Moving such a node down to just
    # above its nearest successor shortens its edges overall, which is
    # the improvement network simplex (dagre's ranker) mostly makes.
    preds = {key: [] for key in nodes}
    succs = {key: [] for key in nodes}
    for src, dst in forward:
        succs[src].append(dst)
        preds[dst].append(src)
    for _ in range(len(nodes)):
        changed = False
        for key in nodes:
            if len(succs[key]) <= len(preds[key]):
                continue
            lowest = min(rank[dst] for dst in succs[key]) - 1
            if rank[key] < lowest:
                rank[key] = lowest
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


def group_paths(paths, edges, routes):
    """Extend `paths` (node key -> the groups it sits in, outermost first)
    to the virtual nodes. An edge inside a block stays inside its frame; one
    into or out of a block runs with the cells of its more deeply nested
    end, rather than being routed all the way around the frame; one between
    two unrelated blocks runs with neither. An edge may name the groups an
    end counts as being in (`src_path`/`dst_path`) when that isn't where
    the node it is laid out to sits -- a flowchart edge to a whole block
    is laid out to a node inside it, but runs outside the frame."""
    extended = dict(paths)
    for index, edge in enumerate(edges):
        if not routes[index]:
            continue
        src = getattr(edge, "src_path", None)
        dst = getattr(edge, "dst_path", None)
        src = paths.get(edge.src, ()) if src is None else src
        dst = paths.get(edge.dst, ()) if dst is None else dst
        deeper, shallower = (src, dst) if len(src) >= len(dst) else (dst, src)
        if deeper[:len(shallower)] == shallower:
            path = deeper
        else:
            path = []
            for a, b in zip(src, dst):
                if a != b:
                    break
                path.append(a)
            path = tuple(path)
        for key in routes[index]:
            extended[key] = path
    return extended


def grouped_sort(layer, score, paths, depth=0, rank=None):
    """Sort `layer` by `score`, keeping every group's members next to each
    other: a group sorts as one block, by its members' mean score, and its
    members are sorted the same way inside it. This is dagre's
    sortSubgraph, and what keeps a node that isn't a block's from landing
    between two that are.

    `rank` (see group_rank) then puts the groups among themselves in one
    order that every layer shares: the slots the groups landed in keep
    their place between the ungrouped cells, and are refilled in rank
    order. Sorted layer by layer, two groups spanning the same layers
    could come out in opposite orders in two of them, and there is no
    placing two frames side by side when each has to be left of the
    other."""
    scores = {cell.key: score(cell) for cell in layer}
    blocks = {}
    for cell in layer:
        path = paths.get(cell.key, ())
        # A cell in no group at this depth is a block of its own.
        block = (True, path[depth]) if len(path) > depth else (False, cell.key)
        blocks.setdefault(block, []).append(cell)
    ordered = []
    for (grouped, _key), members in blocks.items():
        if grouped:
            members = grouped_sort(members, lambda cell: scores[cell.key],
                                   paths, depth + 1, rank)
        mean = sum(scores[cell.key] for cell in members) / len(members)
        ordered.append((mean, grouped, _key, members))
    ordered.sort(key=lambda item: item[0])
    if rank:
        slots = [i for i, item in enumerate(ordered) if item[1]]
        groups = sorted((ordered[i] for i in slots), key=lambda item: rank[item[2]])
        for i, item in zip(slots, groups):
            ordered[i] = item
    return [cell for *_head, members in ordered for cell in members]


def group_rank(layers, paths):
    """One order for all the groups, taken from where their cells sit on
    average across the layers: group -> its index in that order. Only
    siblings are ever compared (grouped_sort works one nesting level at a
    time), so one order over every group serves all levels."""
    seen = {}
    for layer in layers:
        last = max(len(layer) - 1, 1)
        for cell in layer:
            for group in paths.get(cell.key, ()):
                seen.setdefault(group, []).append(cell.order / last)
    # Ties go to first appearance, so the order doesn't depend on hashing.
    ordered = sorted(seen, key=lambda group: sum(seen[group]) / len(seen[group]))
    return {group: index for index, group in enumerate(ordered)}


def order_layers(layers, up, down, passes=8, paths=None):
    """Barycentre sweeps: repeatedly reorder each layer by the average
    position of its neighbours in the layer just processed, each sweep
    followed by `transpose`. A sweep can make things worse as well as
    better, so the ordering with the fewest crossings seen is the one
    kept, the way dagre does it."""
    paths = paths or {}
    cells_by_key = {cell.key: cell for layer in layers for cell in layer}
    for cell_list in layers:
        for position, cell in enumerate(cell_list):
            cell.order = position
        cell_list[:] = grouped_sort(cell_list, lambda cell: cell.order, paths)
        for position, cell in enumerate(cell_list):
            cell.order = position
    rank = group_rank(layers, paths)

    def sweep(neighbours, ordered_layers):
        for cell_list in ordered_layers:
            def barycentre(cell):
                positions = [cells_by_key[k].order for k in neighbours[cell.key]]
                # A cell with no neighbours in the reference layer keeps
                # its current position rather than being swept to the
                # front, which is what a 0 barycentre would do.
                return sum(positions) / len(positions) if positions else cell.order
            cell_list[:] = grouped_sort(cell_list, barycentre, paths, rank=rank)
            for position, cell in enumerate(cell_list):
                cell.order = position

    def settle():
        # A sweep sorts by the rank from before it; this puts every layer
        # in the current one, which is what place() keeps to.
        for cell_list in layers:
            cell_list[:] = grouped_sort(cell_list, lambda cell: cell.order, paths, rank=rank)
            for position, cell in enumerate(cell_list):
                cell.order = position
        transpose(layers, up, down, paths)

    settle()
    best = ([list(layer) for layer in layers], rank, count_crossings(layers, down))
    for index in range(passes):
        if index % 2 == 0:
            sweep(up, layers[1:])
        else:
            sweep(down, layers[-2::-1])
        rank = group_rank(layers, paths)
        settle()
        crossings = count_crossings(layers, down)
        if crossings < best[2]:
            best = ([list(layer) for layer in layers], rank, crossings)
    kept, rank, _crossings = best
    for cell_list, cells in zip(layers, kept):
        cell_list[:] = cells
        for position, cell in enumerate(cell_list):
            cell.order = position
    return rank


def count_crossings(layers, down):
    """How many pairs of edges cross between adjacent layers, as ordered."""
    total = 0
    for upper, lower in zip(layers, layers[1:]):
        position = {cell.key: index for index, cell in enumerate(lower)}
        ends = [(index, position[key]) for index, cell in enumerate(upper)
                for key in down[cell.key] if key in position]
        total += sum(1 for a, (a_top, a_bottom) in enumerate(ends)
                     for b_top, b_bottom in ends[a + 1:]
                     if (a_top - b_top) * (a_bottom - b_bottom) < 0)
    return total


def _pair_crossings(left, right, neighbours):
    """Crossings between the edges of `left` and of `right`, in that order
    in their layer, into one of the layers next to it."""
    return sum(1 for a in neighbours[left.key] for b in neighbours[right.key]
               if a.order > b.order)


def transpose(layers, up, down, paths):
    """Swap neighbours within a layer wherever that crosses fewer edges,
    until no swap helps. Barycentres only look at one side at a time, and
    only at averages, so they leave crossings a single swap undoes. Only
    cells in the same groups are swapped, which keeps every group
    contiguous (see grouped_sort)."""
    cells = {cell.key: cell for layer in layers for cell in layer}
    up = {key: [cells[k] for k in keys] for key, keys in up.items() if key in cells}
    down = {key: [cells[k] for k in keys] for key, keys in down.items() if key in cells}
    improved = True
    while improved:
        improved = False
        for layer in layers:
            for index in range(len(layer) - 1):
                left, right = layer[index], layer[index + 1]
                if paths.get(left.key, ()) != paths.get(right.key, ()):
                    continue
                kept = _pair_crossings(left, right, up) + _pair_crossings(left, right, down)
                swapped = _pair_crossings(right, left, up) + _pair_crossings(right, left, down)
                if swapped < kept:
                    layer[index], layer[index + 1] = right, left
                    left.order, right.order = right.order, left.order
                    improved = True


def _cross_size(cell, vertical):
    return cell.w if vertical else cell.h


def _rank_size(cell, vertical):
    return cell.h if vertical else cell.w


def place(layers, up, down, vertical, rank_gaps, paths=None, rank=None):
    """Assign every cell a centre on both axes.

    Rank axis: layers are stacked by their tallest (widest) member, the
    gap after layer `i` being `rank_gaps[i]`.
    Cross axis: pack each layer, then pull cells toward the average of
    their neighbours in adjacent layers and re-separate. Sweeping in both
    directions keeps a node with only successors (a source) as
    well-aligned as one with only predecessors.

    `paths` (see group_paths) keeps each group's cells together in every
    layer, in the order `rank` (see group_rank) gives the groups, and then
    clears the cross-axis band the group occupies of everything that isn't
    in it (fence_groups).
    """
    paths = paths or {}
    cells_by_key = {cell.key: cell for layer in layers for cell in layer}
    positions = {}

    rank_pos = 0.0
    for layer, gap in zip(layers, rank_gaps):
        extent = max((_rank_size(cell, vertical) for cell in layer), default=0.0)
        for cell in layer:
            positions[cell.key] = [0.0, rank_pos + extent / 2]
            if isinstance(cell, Virtual):
                cell.across = vertical
                if vertical:
                    cell.h = extent
                else:
                    cell.w = extent
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
            separate(layer)

    fence_groups(layers, positions, paths, vertical, rank)

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


def fence_groups(layers, positions, paths, vertical, rank=None):
    """Move cells along the cross axis, as little as possible, until every
    group's cells sit in a band of their own: whatever isn't the group's
    stays clear of it in every layer the group spans.

    Keeping a group's cells contiguous within each layer isn't enough for a
    frame: the frame is one rectangle over all of its layers, and an
    outsider sitting in a layer the group has few cells in -- or none --
    can still land inside it on the cross axis. So, the way dagre's border
    nodes do it, each group gets a left and a right border shared by all
    of its layers, and every layer becomes a sequence -- cells, and the
    borders of the groups it passes through -- whose neighbours must keep
    a minimum distance apart. A group spanning a layer it has no cells in
    still has its borders there: among its sibling groups where `rank`
    puts it, and among the ungrouped cells by which side of its middle
    they are on.

    Since `rank` gives sibling groups one order in every layer, those
    distances form an acyclic graph, and one pass each way solves it: the
    first pushes cells right only as far as they must go, the second left,
    and the average of the two -- both satisfy every distance, so their
    average does too -- keeps the diagram from drifting to one side.
    Pushing cells apart a group at a time instead, until nothing moved,
    could go round in circles: cells squeezed between two frames that
    each pushed them into the other.
    """
    groups = list(dict.fromkeys(group for path in paths.values() for group in path))
    if not groups:
        return
    rank = rank or {group: index for index, group in enumerate(groups)}

    def half(cell):
        return _cross_size(cell, vertical) / 2

    lineage, spans, everyone = {}, {}, {}
    for index, layer in enumerate(layers):
        for cell in layer:
            path = paths.get(cell.key, ())
            for depth, group in enumerate(path):
                lineage.setdefault(group, path[:depth + 1])
                first, _last = spans.get(group, (index, index))
                spans[group] = (first, index)
                everyone.setdefault(group, []).append(positions[cell.key][0])
    children = {}
    for group in sorted(groups, key=lambda group: rank[group]):
        parent = lineage[group][-2] if len(lineage[group]) > 1 else None
        children.setdefault(parent, []).append(group)
    middle = {group: sum(xs) / len(xs) for group, xs in everyone.items()}

    def sequence(group, cells, index):
        """`cells` (the ones inside `group` in this layer, in layer order)
        with the borders of every group they pass through, and of each
        group spanning this layer without a cell in it."""
        depth = len(lineage[group]) if group else 0
        items = []          # a cell, or (child group, its cells here)
        for cell in cells:
            path = paths.get(cell.key, ())
            if len(path) <= depth:
                items.append(cell)
            elif items and isinstance(items[-1], tuple) and items[-1][0] == path[depth]:
                items[-1][1].append(cell)
            else:
                items.append((path[depth], [cell]))
        for child in children.get(group, ()):
            first, last = spans[child]
            if not first <= index <= last or any(
                    isinstance(item, tuple) and item[0] == child for item in items):
                continue
            # Between the sibling groups `rank` puts either side of it ...
            low, high = 0, len(items)
            for at, item in enumerate(items):
                if isinstance(item, tuple):
                    if rank[item[0]] < rank[child]:
                        low = at + 1
                    elif high == len(items):
                        high = at
            # ... and past the ungrouped cells left of its middle.
            at = low
            while at < high and positions[items[at].key][0] < middle[child]:
                at += 1
            items.insert(at, (child, []))
        out = []
        for item in items:
            if isinstance(item, tuple):
                out.append(("<", item[0]))
                out.extend(sequence(item[0], item[1], index))
                out.append((">", item[0]))
            else:
                out.append(item.key)
        return out

    sizes = {cell.key: half(cell) for layer in layers for cell in layer}
    after = {}
    for index, layer in enumerate(layers):
        line = sequence(None, layer, index)
        for a, b in zip(line, line[1:]):
            # A border has no width, and none of the gap either between
            # it and what it frames: a frame's padding is already in the
            # size of its cells (see flowchart's _reserve_cluster_room).
            inside = (isinstance(a, tuple) and a[0] == "<"
                      or isinstance(b, tuple) and b[0] == ">")
            distance = sizes.get(a, 0.0) + sizes.get(b, 0.0) + (0.0 if inside else NODE_GAP)
            links = after.setdefault(a, {})
            links[b] = max(links.get(b, 0.0), distance)

    order = _topological(list(sizes), after)
    if order is None:
        return      # can't happen with a consistent `rank`; leave it be
    before = {}
    for a, links in after.items():
        for b, distance in links.items():
            before.setdefault(b, {})[a] = distance
    rightward, leftward = {}, {}
    for key in order:
        x = positions[key][0] if key in sizes else float("-inf")
        for a, distance in before.get(key, {}).items():
            x = max(x, rightward[a] + distance)
        rightward[key] = x
    for key in reversed(order):
        x = positions[key][0] if key in sizes else float("inf")
        for b, distance in after.get(key, {}).items():
            x = min(x, leftward[b] - distance)
        leftward[key] = x
    for key in sizes:
        positions[key][0] = (rightward[key] + leftward[key]) / 2


def _topological(cells, after):
    """`cells` and every key linked in `after` (key -> {successor: ...}),
    each after everything with a link to it; None if there is a cycle."""
    keys = list(dict.fromkeys(
        list(cells) + [a for a in after] + [b for links in after.values() for b in links]))
    waiting = {key: 0 for key in keys}
    for links in after.values():
        for b in links:
            waiting[b] += 1
    ready = [key for key in keys if not waiting[key]]
    order = []
    while ready:
        key = ready.pop()
        order.append(key)
        for b in after.get(key, ()):
            waiting[b] -= 1
            if not waiting[b]:
                ready.append(b)
    return order if len(order) == len(keys) else None


def mirror(cells, direction, width, height):
    """BT and RL are TD and LR drawn backwards along the rank axis."""
    for cell in cells.values():
        if direction == "BT":
            cell.y = height - cell.y
        elif direction == "RL":
            cell.x = width - cell.x


def layout(nodes, edges, direction, rank_gap=RANK_GAP, edge_room=None,
           groups=None, label_sizes=None, square=None):
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

    `groups` maps a node's key to the groups (a flowchart's subgraphs) it
    sits in, outermost first. Grouped nodes are kept together and
    everything else is kept out of the band they occupy, so a frame fitted
    around them afterwards encloses only its own.

    `label_sizes` maps an edge's index to its label plate's (w, h). An
    edge spanning several ranks has its middle virtual grown to the
    plate's size, the way dagre makes a label a node of its own: the
    layer then keeps a slot for the label beside the boxes in it, rather
    than the label being dropped wherever the edge happens to run (see
    label_anchor).

    `square` is the indices of the edges that will be drawn at right
    angles (see orthogonal_routes), if any. A gap whose runs need more
    tracks than fit in it is widened to fit them.
    """
    vertical = direction in VERTICAL_DIRECTIONS
    rank_nodes(nodes, edges)
    cells, layers, routes = build_layers(nodes, edges)
    for index, (w, h) in (label_sizes or {}).items():
        chain = routes.get(index)
        if chain:
            cell = cells[chain[len(chain) // 2]]
            cell.w, cell.h = max(cell.w, w), max(cell.h, h)
            cell.label = index
    up, down = neighbour_map(edges, cells, routes)
    paths = group_paths(groups or {}, edges, routes)
    rank = order_layers(layers, up, down, paths=paths)
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
    place(layers, up, down, vertical, rank_gaps, paths, rank)
    if square is not None:
        _routes, tracks = orthogonal_routes(
            cells, {index: routes[index] for index in square}, edges, vertical,
            label_sizes)
        shift = 0.0
        for index, gap in enumerate(rank_gaps[:-1]):
            shift += max(0.0, (tracks.get(index, 0) + 1) * TRACK_SPACING - gap)
            for cell in layers[index + 1]:
                if vertical:
                    cell.y += shift
                else:
                    cell.x += shift
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
    x, y = cx + dx * scale, cy + dy * scale
    cap = getattr(cell, "cap", 0.0)
    if cap and abs(abs(y - cy) - half_h) < 1e-6:
        # A cylinder's top and bottom are curved: down (or up) onto the
        # rim, or an edge arriving off-centre stops short of it.
        across = min(1.0, abs(x - cx) / half_w)
        drop = cap * (1 - (1 - across * across) ** 0.5)
        y += drop if y < cy else -drop
    return x, y


def edge_points(cells, routes, index, edge):
    src, dst = cells[edge.src], cells[edge.dst]
    middle = []
    previous = (src.x, src.y)
    for key in routes[index]:
        cell = cells[key]
        # In one side of the virtual's layer and out of the other (see
        # Virtual), whichever side faces the way the edge is coming from.
        if cell.across:
            ends = [(cell.x, cell.y - cell.h / 2), (cell.x, cell.y + cell.h / 2)]
        else:
            ends = [(cell.x - cell.w / 2, cell.y), (cell.x + cell.w / 2, cell.y)]
        ends.sort(key=lambda end: (end[0] - previous[0]) ** 2 + (end[1] - previous[1]) ** 2)
        middle += ends
        previous = ends[-1]
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


# -- orthogonal routing ----------------------------------------------------

# Edges drawn as runs along and across the layers, the way GitHub draws a
# flowchart, rather than as curves.
ORTHOGONAL = "orthogonal"
# Between the ports of one side of a node, at most.
PORT_SPACING = 18.0
# ... and on a side too crowded for that (see _place_ports).
MIN_PORT_SPACING = 12.0
# How far off the middle of its side, as a fraction of half the side, a
# node's ports may shift to line an edge up with its other end.
NEAR_MIDDLE = 0.25
# Ports closer than this across are lined up (see orthogonal_routes).
SNAP = 4.0
# Between two tracks across one gap, at least.
TRACK_SPACING = 12.0
# Clear line kept between two runs sharing a track.
TRACK_CLEARANCE = 6.0
# Radius a route's corners are rounded to, where its segments allow it.
CORNER_RADIUS = 5.0


def rank_bands(cells, vertical):
    """Rank -> (low, high): the stretch of the rank axis its layer spans."""
    bands = {}
    for cell in cells.values():
        along, half = (cell.y, cell.h / 2) if vertical else (cell.x, cell.w / 2)
        low, high = bands.get(cell.rank, (along - half, along + half))
        bands[cell.rank] = (min(low, along - half), max(high, along + half))
    return bands


def orthogonal_routes(cells, routes, edges, vertical, label_sizes=None, bands=None):
    """Edge index -> an orthogonal route, for every edge in `routes`
    between two distinct cells; and the upper rank of each gap -> the
    tracks its runs take up.

    Each edge leaves its node through a port on the side facing the way
    it goes -- one per edge, spread along the side in the order of where
    the edges are headed, so the edges out of one node don't start on top
    of each other and needn't cross on the way out. Between two layers it
    runs straight on if its ends line up, and otherwise along the rank
    axis to a track in the gap, across it, and on into the next layer.
    Runs across one gap that overlap get tracks of their own, stacked so
    that two edges heading the same way don't cross where they needn't
    (`_assign_tracks`). Through a layer it passes, it runs straight
    through its virtual node (see Virtual).

    `label_sizes` (edge index -> plate (w, h)) keeps the ports beside a
    labelled edge's far enough away for its plate not to cover them.
    `bands` (see rank_bands) are the layers' extents if not the cells'
    own: a flowchart's subgraph frames sit in room its nodes were grown by
    for the layout, and a track run through a frame's title band would
    vanish under the title.
    """
    def centre(cell):
        return (cell.x, cell.y) if vertical else (cell.y, cell.x)

    def halves(cell):       # (half across the layer, half along it)
        return (cell.w / 2, cell.h / 2) if vertical else (cell.h / 2, cell.w / 2)

    def point(across, along):
        return (across, along) if vertical else (along, across)

    bands = bands or rank_bands(cells, vertical)

    hops = []       # (edge index, from key, to key)
    for index, edge in enumerate(edges):
        if edge.src == edge.dst or index not in routes:
            continue
        chain = [edge.src] + routes[index] + [edge.dst]
        hops += [(index, a, b) for a, b in zip(chain, chain[1:])]

    def facing(cell, other):
        """+1 if `other` lies further along the rank axis than `cell`."""
        return 1 if centre(other)[1] > centre(cell)[1] else -1

    # Half a label's plate across, for an edge between adjacent layers:
    # its label goes beside its ports. One spanning more has a slot of its
    # own in a layer in between (see layout's `label_sizes`).
    clear = {}
    for index, (w, h) in (label_sizes or {}).items():
        if index in routes and not routes[index]:
            clear[index] = (w if vertical else h) / 2 + TRACK_CLEARANCE
    port, pinned = _place_ports(cells, hops, centre, halves, facing, clear, vertical)
    # Ends a pixel or two out of line would draw as a step too small to
    # read as anything but a glitch: one moves to meet the other, if it is
    # a node's and that keeps it on its side.
    for hop, (_index, a, b) in enumerate(hops):
        offset = port[(hop, 1)] - port[(hop, 0)]
        if not 0 < abs(offset) < SNAP:
            continue
        for end, mine, target in ((1, b, port[(hop, 0)]), (0, a, port[(hop, 1)])):
            cell = cells[mine]
            if not getattr(cell, "virtual", False) and (hop, end) not in pinned \
                    and abs(target - centre(cell)[0]) <= halves(cell)[0] * 0.75:
                port[(hop, end)] = target
                break

    def end_point(key, other, across):
        """Where a hop from `key` toward `other` leaves its outline."""
        cell = cells[key]
        middle, along = centre(cell)
        half_across, half_along = halves(cell)
        side = facing(cell, cells[other])
        offset = min(1.0, abs(across - middle) / half_across) if half_across else 0.0
        shape = getattr(cell, "shape", None)
        if shape == "diamond":
            half_along *= 1 - offset
        elif shape == "circle":
            half_along *= (1 - offset * offset) ** 0.5
        elif getattr(cell, "cap", 0.0) and vertical:
            half_along -= cell.cap * (1 - (1 - offset * offset) ** 0.5)
        return across, along + side * half_along

    pieces = {}
    runs = {}       # upper rank of a gap -> [(hop, from across, to across)]
    for hop, (index, a, b) in enumerate(hops):
        start = end_point(a, b, port[(hop, 0)])
        end = end_point(b, a, port[(hop, 1)])
        pieces[hop] = [start, end]
        if abs(start[0] - end[0]) > 0.5:
            upper, lower = sorted((cells[a].rank, cells[b].rank))
            from_across, to_across = (start[0], end[0]) if cells[a].rank == upper \
                else (end[0], start[0])
            runs.setdefault(upper, []).append((hop, from_across, to_across))
    counts = {}
    for upper, gap_runs in runs.items():
        tracks = _assign_tracks(gap_runs)
        counts[upper] = count = max(tracks.values()) + 1
        # The gap's edge on the upper rank's side first: its high end, unless
        # the diagram is drawn backwards (BT, RL) and the ranks run the
        # other way.
        if sum(bands[upper]) <= sum(bands[upper + 1]):
            top, bottom = bands[upper][1], bands[upper + 1][0]
        else:
            top, bottom = bands[upper][0], bands[upper + 1][1]
        for hop, from_across, to_across in gap_runs:
            along = top + (bottom - top) * (tracks[hop] + 1) / (count + 1)
            start, end = pieces[hop]
            pieces[hop] = [start, (start[0], along), (end[0], along), end]

    out = {}
    for hop, (index, a, b) in enumerate(hops):
        out.setdefault(index, []).extend(pieces[hop])
    return ({index: [point(*p) for p in _dedupe(points)] for index, points in out.items()},
            counts)


def _pointed(cell, vertical):
    """Whether `cell`'s sides facing along the rank axis are curved or
    come to a point: a diamond's, a circle's, and an upright cylinder's
    top and bottom."""
    return (getattr(cell, "shape", None) in ("diamond", "circle")
            or bool(getattr(cell, "cap", 0.0)) and vertical)


def _place_ports(cells, hops, centre, halves, facing, clear, vertical):
    """(hop, end) -> where across its layer the hop meets its cell.

    A virtual is met where it is. A node's side gets one port per hop
    attached to it, gathered about the middle of the side the way GitHub
    attaches its edges, kept in the order of the hops' other ends, so the
    hops leaving one side don't cross each other, and PORT_SPACING apart,
    or as far apart as the side has room for. A labelled hop's port keeps
    `clear` (edge index -> distance) from its neighbours as well, so far as
    the side allows. The group may shift off the middle, by no more than
    NEAR_MIDDLE of the side, where that lines a port up with its other end
    and so draws its hop straight. Each side's order and shift depend on
    where the other ends are, which move as they are placed in turn, so it
    takes a few rounds to settle.

    The one exception is a lone hop on a side that is curved or pointed
    (`_pointed`): it meets the middle, the top of the rim or the vertex.
    Anywhere else it lands on a slope and looks like a near miss.

    Returns the ports and the set of those pinned like that.
    """
    port = {}
    sides = {}
    for hop, (_index, a, b) in enumerate(hops):
        for end, mine, other in ((0, a, b), (1, b, a)):
            port[(hop, end)] = centre(cells[mine])[0]
            if not getattr(cells[mine], "virtual", False):
                key = (mine, facing(cells[mine], cells[other]))
                sides.setdefault(key, []).append((hop, end))
    pinned = {attached[0] for (key, _side), attached in sides.items()
              if len(attached) == 1 and _pointed(cells[key], vertical)}
    for _round in range(3):
        for (key, _side), attached in sides.items():
            if attached[0] in pinned:
                continue
            cell = cells[key]
            middle = centre(cell)[0]
            # Not right out to the corners, and on a diamond or a circle
            # no further out than its outline is still facing this way.
            half = halves(cell)[0] * (0.5 if getattr(cell, "shape", None)
                                      in ("diamond", "circle") else 0.75)
            low, high = middle - half, middle + half
            wanted = sorted((port[(hop, 1 - end)], hop, end) for hop, end in attached)
            others = [w[0] for w in wanted]
            room = [clear.get(hops[hop][0], 0.0) for _other, hop, _end in wanted]
            near = NEAR_MIDDLE * halves(cell)[0]
            spots = None
            # A tighter spacing on a crowded side, or one that leaves the
            # labels less room than they want, is worth it if it keeps more
            # edges straight.
            for spacing in (PORT_SPACING, MIN_PORT_SPACING):
                step = min(spacing, 2 * half / (len(wanted) - 1)) if len(wanted) > 1 else 0.0
                apart = [max(step, room[i] + room[i + 1]) for i in range(len(wanted) - 1)]
                # Labels wanting more room than the side has will have to
                # find it further along their edges anyway (see
                # place_labels).
                roomy = sum(apart) <= high - low
                for labelled in {roomy, False}:
                    gaps = apart if labelled else [step] * len(apart)
                    slots = [0.0]
                    for gap in gaps:
                        slots.append(slots[-1] + gap)
                    slots = [middle + slot - slots[-1] / 2 for slot in slots]
                    # As long as the group still straddles the middle.
                    reach = near + (slots[-1] - slots[0]) / 2
                    shifts = {0.0} | {other - slot for other, slot in zip(others, slots)}
                    for shift in shifts:
                        if abs(shift) > reach or slots[0] + shift < low - 0.5 \
                                or slots[-1] + shift > high + 0.5:
                            continue
                        tried = [slot + shift for slot in slots]
                        straight = sum(abs(spot - other) < 0.5
                                       for spot, other in zip(tried, others))
                        score = (straight, labelled, -abs(shift), spacing)
                        if spots is None or score > best:
                            spots, best = tried, score
            for spot, (_other, hop, end) in zip(spots, wanted):
                port[(hop, end)] = spot
    return port, pinned


def _dedupe(points):
    out = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out


def _assign_tracks(gap_runs):
    """Hop -> track (0 nearest the upper layer) for runs across one gap,
    given as (hop, across where it leaves the upper layer, across where it
    enters the lower).

    Two runs heading right cross needlessly if the one ending further left
    is on the higher track: its drop into the lower layer cuts through
    the other's run. So runs heading right are stacked by where they end,
    furthest right highest, runs heading left the mirror image, and each
    takes the highest track where it overlaps nothing already there.
    """
    rightward = sorted((r for r in gap_runs if r[2] > r[1]), key=lambda r: -r[2])
    leftward = sorted((r for r in gap_runs if r[2] < r[1]), key=lambda r: r[2])
    tracks, taken = {}, []
    for hop, a, b in rightward + leftward:
        low, high = min(a, b) - TRACK_CLEARANCE, max(a, b) + TRACK_CLEARANCE
        track = 0
        while track < len(taken) and any(l < high and low < h for l, h in taken[track]):
            track += 1
        if track == len(taken):
            taken.append([])
        taken[track].append((low, high))
        tracks[hop] = track
    return tracks


def run_anchor(points, vertical):
    """The middle of a route's longest run along the rank axis: where a
    label goes on a right-angled edge. A run across a gap is the worst
    place for one -- the plate covers the tracks either side of it, where
    other edges run -- while the runs along are where the gap was sized
    for labels (layout's `edge_room`). None if the route has no such run."""
    best, best_length = None, 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if (x1 == x2) if vertical else (y1 == y2):
            length = abs(y2 - y1) if vertical else abs(x2 - x1)
            if length > best_length:
                best, best_length = ((x1 + x2) / 2, (y1 + y2) / 2), length
    return best


def crossings(paths, vertical):
    """Key -> {segment index: [distances from the segment's start]}: where
    each path's runs *across* the layers cross another path's run along
    them, the places `rounded` draws a hop. `paths` maps a key to an
    orthogonal route (see orthogonal_routes).

    Only the runs across hop, the way GitHub draws it: across is where an
    edge goes out of its way, and along is the line a reader follows from
    one layer to the next. A run meeting another only at an end -- a T, or
    two edges into one port -- is no crossing.
    """
    across, along = [], []
    for key, points in paths.items():
        for index, ((x1, y1), (x2, y2)) in enumerate(zip(points, points[1:])):
            if abs(y1 - y2) < 1e-6 and abs(x1 - x2) > 1e-6:
                (across if vertical else along).append((key, index, x1, y1, x2, y2))
            elif abs(x1 - x2) < 1e-6 and abs(y1 - y2) > 1e-6:
                (along if vertical else across).append((key, index, x1, y1, x2, y2))
    found = {}
    for key, index, x1, y1, x2, y2 in across:
        for other, _index, u1, v1, u2, v2 in along:
            if other == key:
                continue
            if vertical:    # across horizontal, along vertical
                x, y = u1, y1
                hit = (min(x1, x2) + 0.5 < x < max(x1, x2) - 0.5
                       and min(v1, v2) + 0.5 < y < max(v1, v2) - 0.5)
                distance = abs(x - x1)
            else:
                x, y = x1, v1
                hit = (min(y1, y2) + 0.5 < y < max(y1, y2) - 0.5
                       and min(u1, u2) + 0.5 < x < max(u1, u2) - 0.5)
                distance = abs(y - y1)
            if hit:
                found.setdefault(key, {}).setdefault(index, []).append(distance)
    return found


def rounded(points, radius=CORNER_RADIUS, hops=None):
    """`points` with each corner rounded off to a quarter circle of up to
    `radius` -- less where a segment is too short to take it -- flattened
    into straight pieces like `curved`'s, so heads and labels work on it
    the same way. The first and last segments stay as they are.

    `hops` (segment index -> distances along it, see crossings) are where
    another line crosses; the segment jumps it with a half circle of
    HOP_RADIUS, bulging up (or left). Crossings closer together than one
    hop's width are jumped as one, and one too close to a corner to fit a
    hop is drawn plain.
    """
    hops = hops or {}
    radii = [0.0] * len(points)
    for i in range(1, len(points) - 1):
        r = min(radius, _distance(points[i - 1], points[i]) / 2,
                _distance(points[i], points[i + 1]) / 2)
        radii[i] = r if r >= 0.5 else 0.0
    out = [points[0]]
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(points, points[1:])):
        if i and radii[i]:
            out += _corner(points[i - 1], points[i], points[i + 1], radii[i])
        elif i:
            out.append(points[i])
        length = _distance((x0, y0), (x1, y1))
        if not length or i not in hops:
            continue
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        nx, ny = (0.0, -1.0) if abs(uy) < 1e-6 else (-1.0, 0.0)
        room_from, room_to = radii[i] + 0.5, length - radii[i + 1] - 0.5
        for start, end in _hop_groups(sorted(hops[i])):
            if start - HOP_RADIUS < room_from or end + HOP_RADIUS > room_to:
                continue
            for centre, first, last in ((start, math.pi, math.pi / 2), (end, math.pi / 2, 0.0)):
                for step in range(_HOP_STEPS + 1):
                    angle = first + (last - first) * step / _HOP_STEPS
                    along = centre + HOP_RADIUS * math.cos(angle)
                    up = HOP_RADIUS * math.sin(angle)
                    out.append((x0 + ux * along + nx * up, y0 + uy * along + ny * up))
    out.append(points[-1])
    return out


# A line-crossing hop's radius, and the straight pieces each quarter of
# it is drawn as.
HOP_RADIUS = 4.0
_HOP_STEPS = 4


def _distance(a, b):
    return ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5


def _corner(before, corner, after, r):
    """A rounded corner: a quadratic from r before it to r after, the
    corner its control point -- close enough to an arc at this size."""
    (x0, y0), (x1, y1), (x2, y2) = before, corner, after
    a, b = _distance(before, corner), _distance(corner, after)
    p = (x1 + (x0 - x1) * r / a, y1 + (y0 - y1) * r / a)
    q = (x1 + (x2 - x1) * r / b, y1 + (y2 - y1) * r / b)
    return [((1 - t) ** 2 * p[0] + 2 * (1 - t) * t * x1 + t * t * q[0],
             (1 - t) ** 2 * p[1] + 2 * (1 - t) * t * y1 + t * t * q[1])
            for t in (step / 4 for step in range(5))]


def _hop_groups(distances):
    """Sorted crossing distances -> (first, last) of each run of them too
    close together to hop one at a time."""
    groups = []
    for d in distances:
        if groups and d - groups[-1][1] < 2 * HOP_RADIUS + 2:
            groups[-1][1] = d
        else:
            groups.append([d, d])
    return [tuple(g) for g in groups]


# Mermaid's own default for flowcharts; "linear" is the straight route.
DEFAULT_CURVE = "basis"
# Straight pieces each cubic of a curved edge is drawn as. A span is one
# rank gap or so, a hundred-odd pixels, where this is smooth at any zoom.
_CURVE_STEPS = 16


def curved(points, curve=DEFAULT_CURVE, boxes=()):
    """The polyline an edge is drawn as: `points` themselves for "linear",
    otherwise a uniform cubic B-spline through them -- d3's curveBasis,
    which mermaid draws its edges with by default.

    The spline is flattened here rather than handed to cairo as curves, so
    everything downstream of a scene.Line -- arrowheads off its last
    segment, the scene's extents, label placement -- keeps working on
    straight segments. It starts and ends on the route's end points, leaves
    and arrives along the route's first and last segments (so heads point
    as they did).

    Rounding a corner cuts across it, though, and a box sitting just
    inside a corner -- a node beside the virtual one an edge bends at --
    gets cut through. `boxes` are the (x, y, w, h) the edge must stay out
    of (every box but its own two ends'). One that would be crossed gets a
    tighter curve, each corner counted twice so the spline hugs it, and
    failing that the straight route, which the layout has kept clear.
    """
    if curve == "linear" or len(points) < 3:
        return list(points)
    for multiplicity in (1, 2):
        inner = [point for point in points[1:-1] for _ in range(multiplicity)]
        out = _basis([points[0]] * 3 + inner + [points[-1]] * 3)
        if not any(x < px < x + w and y < py < y + h
                   for x, y, w, h in boxes for px, py in out):
            return out
    return list(points)


def _basis(controls):
    """Flatten the uniform cubic B-spline over `controls`, whose first and
    last points are each repeated three times so it ends on them."""
    windows = list(zip(controls, controls[1:], controls[2:], controls[3:]))
    out = [controls[0]]
    for index, ((x0, y0), (x1, y1), (x2, y2), (x3, y3)) in enumerate(windows):
        # The first and last pieces are straight: the edge's way out of
        # and into its boxes, the stretch the arrowheads point along.
        steps = 1 if index in (0, len(windows) - 1) else _CURVE_STEPS
        for step in range(1, steps + 1):
            t = step / steps
            b0 = (1 - t) ** 3 / 6
            b1 = (3 * t ** 3 - 6 * t ** 2 + 4) / 6
            b2 = (-3 * t ** 3 + 3 * t ** 2 + 3 * t + 1) / 6
            b3 = t ** 3 / 6
            out.append((b0 * x0 + b1 * x1 + b2 * x2 + b3 * x3,
                        b0 * y0 + b1 * y1 + b2 * y2 + b3 * y3))
    out[-1] = controls[-1]
    return out


def _nearest_on(path, point):
    """The point of polyline `path` nearest to `point`."""
    px, py = point
    best, best_d = path[0], None
    for (x1, y1), (x2, y2) in zip(path, path[1:]):
        dx, dy = x2 - x1, y2 - y1
        span = dx * dx + dy * dy
        t = 0.0 if span == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / span))
        x, y = x1 + dx * t, y1 + dy * t
        d = (x - px) ** 2 + (y - py) ** 2
        if best_d is None or d < best_d:
            best, best_d = (x, y), d
    return best


def label_anchor(cells, routes, index):
    """The centre of the slot layout() kept for edge `index`'s label, or
    None if it has none."""
    for key in routes.get(index, ()):
        cell = cells[key]
        if cell.label == index:
            return cell.x, cell.y
    return None


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


def place_labels(requests, obstacles=(), lines=()):
    """Centres for edge labels that don't sit on top of each other.

    `requests` is a list of (edge points, plate width, plate height), or
    of (route, width, height, drawn path) for a curved edge: positions are
    chosen along the straight route, whose segments say where the room is,
    and then moved onto the curve actually drawn. A fifth item, if
    present and not None, is the spot to try first instead of
    `label_position` (see label_anchor). And
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

    `lines` are (owner, polyline) for the lines drawn under the plates,
    `owner` the index of the request that line is the edge of, or None. A
    plate hides whatever line it covers, so where it has the choice it
    covers as little of the other edges as it can -- the right-angled
    routes of orthogonal_routes run side by side on tracks a few pixels
    apart, and a label centred on one easily blots out the next.
    """
    taken = list(obstacles)
    centres = []
    strokes = [(owner, (min(x1, x2) - 1.0, min(y1, y2) - 1.0,
                        abs(x2 - x1) + 2.0, abs(y2 - y1) + 2.0))
               for owner, line in lines for (x1, y1), (x2, y2) in zip(line, line[1:])]
    for request, (points, w, h, *rest) in enumerate(requests):
        drawn = rest[0] if rest else None
        anchor = rest[1] if len(rest) > 1 else None
        candidates = [anchor or label_position(points)]
        candidates += [_point_along(points, f) for f in _LABEL_FRACTIONS]
        if drawn is not None and drawn is not points:
            candidates = [_nearest_on(drawn, c) for c in candidates]
        best, best_cost = None, None
        for cx, cy in candidates:
            plate = (cx - w / 2 - LABEL_CLEARANCE, cy - h / 2 - LABEL_CLEARANCE,
                     w + 2 * LABEL_CLEARANCE, h + 2 * LABEL_CLEARANCE)
            cost = sum(_overlap_area(plate, box) for box in taken)
            cost += sum(_overlap_area(plate, box) for owner, box in strokes
                        if owner != request)
            if best_cost is None or cost < best_cost:
                best, best_cost = (cx, cy, plate), cost
            if cost == 0:
                break
        cx, cy, plate = best
        taken.append(plate)
        centres.append((cx, cy))
    return centres
