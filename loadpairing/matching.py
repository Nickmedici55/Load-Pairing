"""Maximum-weight matching over the graph of feasible pairs.

The spec calls for ``networkx.max_weight_matching(G, maxcardinality=True)``
and that is what runs when networkx is installed. The pure-Python blossom
implementation below is the fallback so the tool still produces the same plan
on a machine with nothing installed; it is a port of Galil's O(n^3)
primal-dual algorithm and is verified against brute force in the test suite.

Weights are integers. Callers scale hours to hundredths before matching so the
dual arithmetic stays exact.
"""

from __future__ import annotations

from typing import Hashable, Iterable, Sequence

Edge = tuple[Hashable, Hashable, int]


def max_weight_matching(
    edges: Iterable[Edge],
    maxcardinality: bool = True,
    prefer: str = "auto",
) -> tuple[set[frozenset], str]:
    """Return ``(matching, matcher_name)``.

    ``matching`` is a set of frozen vertex pairs. ``prefer`` is ``"auto"``,
    ``"networkx"`` or ``"builtin"``.
    """
    edges = [(u, v, int(w)) for u, v, w in edges]

    if prefer in ("auto", "networkx"):
        try:
            import networkx as nx
        except ImportError:
            if prefer == "networkx":
                raise
        else:
            graph = nx.Graph()
            for u, v, w in edges:
                graph.add_edge(u, v, weight=w)
            pairs = nx.max_weight_matching(graph, maxcardinality=maxcardinality)
            return {frozenset(pair) for pair in pairs}, "networkx"

    return blossom(edges, maxcardinality=maxcardinality), "builtin"


def blossom(edges: Sequence[Edge], maxcardinality: bool = True) -> set[frozenset]:
    """Exact maximum-weight matching on a general graph."""
    if not edges:
        return set()

    nodes: list[Hashable] = []
    index: dict[Hashable, int] = {}
    for u, v, _ in edges:
        for node in (u, v):
            if node not in index:
                index[node] = len(nodes)
                nodes.append(node)

    numbered = [(index[u], index[v], w) for u, v, w in edges]
    mate = _blossom_numbered(len(nodes), numbered, maxcardinality)
    return {frozenset((nodes[v], nodes[m])) for v, m in enumerate(mate) if m >= 0}


def _blossom_numbered(nvertex: int, edges: Sequence[tuple[int, int, int]], maxcardinality: bool) -> list[int]:
    """Galil's primal-dual blossom algorithm over vertices ``0..nvertex-1``."""
    nedge = len(edges)
    maxweight = max(w for _, _, w in edges)

    # endpoint[2*k] and endpoint[2*k+1] are the two ends of edge k.
    endpoint = [edges[k // 2][k % 2] for k in range(2 * nedge)]

    # neighbend[v] holds, for every edge at v, the endpoint index of its far end.
    neighbend: list[list[int]] = [[] for _ in range(nvertex)]
    for k, (i, j, _) in enumerate(edges):
        neighbend[i].append(2 * k + 1)
        neighbend[j].append(2 * k)

    mate = [-1] * nvertex                       # mate[v] = endpoint of v's matched edge

    # Blossoms are numbered nvertex..2*nvertex-1; single vertices are trivial ones.
    label = [0] * (2 * nvertex)                 # 0 free, 1 S (outer), 2 T (inner), 5 breadcrumb
    labelend = [-1] * (2 * nvertex)
    inblossom = list(range(nvertex)) + [-1] * nvertex
    blossomparent = [-1] * (2 * nvertex)
    blossomchilds: list[list[int] | None] = [None] * (2 * nvertex)
    blossombase = list(range(nvertex)) + [-1] * nvertex
    blossomendps: list[list[int] | None] = [None] * (2 * nvertex)
    bestedge = [-1] * (2 * nvertex)
    blossombestedges: list[list[int] | None] = [None] * (2 * nvertex)
    unusedblossoms = list(range(nvertex, 2 * nvertex))
    dualvar = [maxweight] * nvertex + [0] * nvertex
    allowedge = [False] * nedge
    queue: list[int] = []

    def slack(k: int) -> int:
        i, j, wt = edges[k]
        return dualvar[i] + dualvar[j] - 2 * wt

    def blossom_leaves(b: int):
        if b < nvertex:
            yield b
            return
        stack = list(blossomchilds[b])
        while stack:
            child = stack.pop()
            if child < nvertex:
                yield child
            else:
                stack.extend(blossomchilds[child])

    def assign_label(w: int, t: int, p: int) -> None:
        """Label the blossom containing ``w`` with ``t``, entered through ``p``."""
        b = inblossom[w]
        label[w] = label[b] = t
        labelend[w] = labelend[b] = p
        bestedge[w] = bestedge[b] = -1
        if t == 1:
            queue.extend(blossom_leaves(b))
        elif t == 2:
            base = blossombase[b]
            assign_label(endpoint[mate[base]], 1, mate[base] ^ 1)

    def scan_blossom(v: int, w: int) -> int:
        """Walk both alternating paths back; return a common base or -1."""
        path: list[int] = []
        base = -1
        while v != -1 or w != -1:
            b = inblossom[v]
            if label[b] & 4:
                base = blossombase[b]
                break
            path.append(b)
            label[b] = 5
            if labelend[b] == -1:
                v = -1
            else:
                v = endpoint[labelend[b]]
                v = endpoint[labelend[inblossom[v]]]
            if w != -1:
                v, w = w, v
        for b in path:
            label[b] = 1
        return base

    def add_blossom(base: int, k: int) -> None:
        """Contract the odd cycle closed by edge ``k`` into a new S-blossom."""
        v, w, _ = edges[k]
        bb = inblossom[base]
        bv, bw = inblossom[v], inblossom[w]

        b = unusedblossoms.pop()
        blossombase[b] = base
        blossomparent[b] = -1
        blossomparent[bb] = b

        path = blossomchilds[b] = []
        endps = blossomendps[b] = []

        while bv != bb:                     # v side, back to the base
            blossomparent[bv] = b
            path.append(bv)
            endps.append(labelend[bv])
            v = endpoint[labelend[bv]]
            bv = inblossom[v]
        path.append(bb)
        path.reverse()
        endps.reverse()
        endps.append(2 * k)

        while bw != bb:                     # w side, back to the base
            blossomparent[bw] = b
            path.append(bw)
            endps.append(labelend[bw] ^ 1)
            w = endpoint[labelend[bw]]
            bw = inblossom[w]

        label[b] = 1
        labelend[b] = labelend[bb]
        dualvar[b] = 0
        for leaf in blossom_leaves(b):
            if label[inblossom[leaf]] == 2:
                queue.append(leaf)          # T-vertices become S inside the blossom
            inblossom[leaf] = b

        # Merge the children's best-edge lists into one for the new blossom.
        best: dict[int, int] = {}
        for child in path:
            if blossombestedges[child] is None:
                child_edges = [
                    p // 2 for leaf in blossom_leaves(child) for p in neighbend[leaf]
                ]
            else:
                child_edges = blossombestedges[child]
            for edge in child_edges:
                i, j, _ = edges[edge]
                far = j if inblossom[i] == b else i
                bfar = inblossom[far]
                if bfar != b and label[bfar] == 1 and (bfar not in best or slack(edge) < slack(best[bfar])):
                    best[bfar] = edge
            blossombestedges[child] = None
            bestedge[child] = -1

        blossombestedges[b] = list(best.values())
        bestedge[b] = -1
        for edge in blossombestedges[b]:
            if bestedge[b] == -1 or slack(edge) < slack(bestedge[b]):
                bestedge[b] = edge

    def expand_blossom(b: int, endstage: bool) -> None:
        """Undo a blossom, either at the end of a stage or when its dual hits 0."""
        for child in blossomchilds[b]:
            blossomparent[child] = -1
            if child < nvertex:
                inblossom[child] = child
            elif endstage and dualvar[child] == 0:
                expand_blossom(child, endstage)
            else:
                for leaf in blossom_leaves(child):
                    inblossom[leaf] = child

        if not endstage and label[b] == 2:
            # Relabel the alternating path through the blossom.
            entrychild = inblossom[endpoint[labelend[b] ^ 1]]
            j = blossomchilds[b].index(entrychild)
            if j & 1:
                j -= len(blossomchilds[b])
                jstep, endptrick = 1, 0
            else:
                jstep, endptrick = -1, 1
            p = labelend[b]
            while j != 0:
                label[endpoint[p ^ 1]] = 0
                label[endpoint[blossomendps[b][j - endptrick] ^ endptrick ^ 1]] = 0
                assign_label(endpoint[p ^ 1], 2, p)
                allowedge[blossomendps[b][j - endptrick] // 2] = True
                j += jstep
                p = blossomendps[b][j - endptrick] ^ endptrick
                allowedge[p // 2] = True
                j += jstep

            bv = blossomchilds[b][j]
            label[endpoint[p ^ 1]] = label[bv] = 2
            labelend[endpoint[p ^ 1]] = labelend[bv] = p
            bestedge[bv] = -1

            j += jstep
            while blossomchilds[b][j] != entrychild:
                bv = blossomchilds[b][j]
                if label[bv] == 1:
                    j += jstep
                    continue
                for leaf in blossom_leaves(bv):
                    if label[leaf]:
                        break
                else:
                    leaf = -1
                if leaf >= 0:
                    label[leaf] = 0
                    label[endpoint[mate[blossombase[bv]]]] = 0
                    assign_label(leaf, 2, labelend[leaf])
                j += jstep

        label[b] = labelend[b] = -1
        blossomchilds[b] = blossomendps[b] = None
        blossombase[b] = -1
        blossombestedges[b] = None
        bestedge[b] = -1
        unusedblossoms.append(b)

    def augment_blossom(b: int, v: int) -> None:
        """Rotate a blossom so that ``v`` becomes its base."""
        t = v
        while blossomparent[t] != b:
            t = blossomparent[t]
        if t >= nvertex:
            augment_blossom(t, v)

        i = j = blossomchilds[b].index(t)
        if i & 1:
            j -= len(blossomchilds[b])
            jstep, endptrick = 1, 0
        else:
            jstep, endptrick = -1, 1

        while j != 0:
            j += jstep
            t = blossomchilds[b][j]
            p = blossomendps[b][j - endptrick] ^ endptrick
            if t >= nvertex:
                augment_blossom(t, endpoint[p])
            j += jstep
            t = blossomchilds[b][j]
            if t >= nvertex:
                augment_blossom(t, endpoint[p ^ 1])
            mate[endpoint[p]] = p ^ 1
            mate[endpoint[p ^ 1]] = p

        blossomchilds[b] = blossomchilds[b][i:] + blossomchilds[b][:i]
        blossomendps[b] = blossomendps[b][i:] + blossomendps[b][:i]
        blossombase[b] = blossombase[blossomchilds[b][0]]

    def augment_matching(k: int) -> None:
        """Flip the augmenting path found through edge ``k``."""
        v, w, _ = edges[k]
        for s, p in ((v, 2 * k + 1), (w, 2 * k)):
            while True:
                bs = inblossom[s]
                if bs >= nvertex:
                    augment_blossom(bs, s)
                mate[s] = p
                if labelend[bs] == -1:
                    break
                t = endpoint[labelend[bs]]
                bt = inblossom[t]
                s = endpoint[labelend[bt]]
                j = endpoint[labelend[bt] ^ 1]
                if bt >= nvertex:
                    augment_blossom(bt, j)
                mate[j] = labelend[bt]
                p = labelend[bt] ^ 1

    for _stage in range(nvertex):
        label[:] = [0] * (2 * nvertex)
        bestedge[:] = [-1] * (2 * nvertex)
        blossombestedges[nvertex:] = [None] * nvertex
        allowedge[:] = [False] * nedge
        queue.clear()

        for v in range(nvertex):
            if mate[v] == -1 and label[inblossom[v]] == 0:
                assign_label(v, 1, -1)

        augmented = False
        while True:
            while queue and not augmented:
                v = queue.pop()
                for p in neighbend[v]:
                    k = p // 2
                    w = endpoint[p]
                    if inblossom[v] == inblossom[w]:
                        continue           # both ends inside the same blossom

                    kslack = 0
                    if not allowedge[k]:
                        kslack = slack(k)
                        if kslack <= 0:
                            allowedge[k] = True

                    if allowedge[k]:
                        if label[inblossom[w]] == 0:
                            assign_label(w, 2, p ^ 1)
                        elif label[inblossom[w]] == 1:
                            base = scan_blossom(v, w)
                            if base >= 0:
                                add_blossom(base, k)
                            else:
                                augment_matching(k)
                                augmented = True
                                break
                        elif label[w] == 0:
                            label[w] = 2
                            labelend[w] = p ^ 1
                    elif label[inblossom[w]] == 1:
                        b = inblossom[v]   # least-slack edge out of this S-blossom
                        if bestedge[b] == -1 or kslack < slack(bestedge[b]):
                            bestedge[b] = k
                    elif label[w] == 0:
                        if bestedge[w] == -1 or kslack < slack(bestedge[w]):
                            bestedge[w] = k

            if augmented:
                break

            deltatype = -1
            delta = deltaedge = deltablossom = None

            if not maxcardinality:
                deltatype = 1
                delta = min(dualvar[:nvertex])

            for v in range(nvertex):
                if label[inblossom[v]] == 0 and bestedge[v] != -1:
                    d = slack(bestedge[v])
                    if deltatype == -1 or d < delta:
                        delta, deltatype, deltaedge = d, 2, bestedge[v]

            for b in range(2 * nvertex):
                if blossomparent[b] == -1 and label[b] == 1 and bestedge[b] != -1:
                    kslack = slack(bestedge[b])
                    d = kslack // 2
                    if deltatype == -1 or d < delta:
                        delta, deltatype, deltaedge = d, 3, bestedge[b]

            for b in range(nvertex, 2 * nvertex):
                if blossombase[b] >= 0 and blossomparent[b] == -1 and label[b] == 2:
                    if deltatype == -1 or dualvar[b] < delta:
                        delta, deltatype, deltablossom = dualvar[b], 4, b

            if deltatype == -1:
                deltatype = 1
                delta = max(0, min(dualvar[:nvertex]))

            for v in range(nvertex):                # shift the duals
                if label[inblossom[v]] == 1:
                    dualvar[v] -= delta
                elif label[inblossom[v]] == 2:
                    dualvar[v] += delta
            for b in range(nvertex, 2 * nvertex):
                if blossombase[b] >= 0 and blossomparent[b] == -1:
                    if label[b] == 1:
                        dualvar[b] += delta
                    elif label[b] == 2:
                        dualvar[b] -= delta

            if deltatype == 1:
                break                                # no further improvement
            if deltatype == 2:
                allowedge[deltaedge] = True
                i, j, _ = edges[deltaedge]
                if label[inblossom[i]] == 0:
                    i, j = j, i
                queue.append(i)
            elif deltatype == 3:
                allowedge[deltaedge] = True
                i, _j, _ = edges[deltaedge]
                queue.append(i)
            else:
                expand_blossom(deltablossom, False)

        if not augmented:
            break

        for b in range(nvertex, 2 * nvertex):        # end of stage: expand zero-dual blossoms
            if (
                blossomparent[b] == -1
                and blossombase[b] >= 0
                and label[b] == 1
                and dualvar[b] == 0
            ):
                expand_blossom(b, True)

    return [endpoint[mate[v]] if mate[v] >= 0 else -1 for v in range(nvertex)]
