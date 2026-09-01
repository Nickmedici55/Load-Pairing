"""The built-in blossom matcher is checked against an exact subset DP.

The DP is exponential but trivially correct, which is what makes it a useful
oracle for the primal-dual implementation the tool actually uses.
"""

import itertools
import random
import unittest

from loadpairing.matching import blossom, max_weight_matching


def exact(n, edges, maxcardinality):
    """Best (cardinality, weight) over every matching, by DP over subsets."""
    weight = {}
    for u, v, w in edges:
        key = (min(u, v), max(u, v))
        weight[key] = max(weight.get(key, w), w)

    def solve(compare_cardinality):
        best = [None] * (1 << n)
        best[0] = (0, 0)
        for mask in range(1 << n):
            if best[mask] is None:
                continue
            free = next((i for i in range(n) if not mask >> i & 1), None)
            if free is None:
                continue
            for nxt, gain in [(mask | 1 << free, (0, 0))] + [
                ((mask | 1 << free | 1 << other), (1, weight[(free, other)]))
                for other in range(free + 1, n)
                if not mask >> other & 1 and (free, other) in weight
            ]:
                candidate = (best[mask][0] + gain[0], best[mask][1] + gain[1])
                key = candidate if compare_cardinality else (0, candidate[1])
                if best[nxt] is None or key > (
                    best[nxt] if compare_cardinality else (0, best[nxt][1])
                ):
                    best[nxt] = candidate
        return best[(1 << n) - 1]

    total = solve(maxcardinality)
    return total if maxcardinality else (0, total[1])


def score(matching, edges):
    weight = {frozenset((u, v)): w for u, v, w in edges}
    return len(matching), sum(weight[pair] for pair in matching)


class BlossomTest(unittest.TestCase):
    def test_an_empty_graph_matches_nothing(self):
        self.assertEqual(blossom([]), set())

    def test_a_single_edge(self):
        self.assertEqual(blossom([("a", "b", 5)]), {frozenset(("a", "b"))})

    def test_a_triangle_keeps_the_heaviest_edge(self):
        edges = [("a", "b", 5), ("b", "c", 9), ("a", "c", 7)]
        self.assertEqual(blossom(edges), {frozenset(("b", "c"))})

    def test_max_cardinality_beats_weight(self):
        # Covering every vertex costs weight here; maxcardinality pays it.
        edges = [("a", "b", 100), ("c", "d", -5)]
        self.assertEqual(len(blossom(edges, maxcardinality=True)), 2)
        self.assertEqual(blossom(edges, maxcardinality=False), {frozenset(("a", "b"))})

    def test_a_five_cycle_is_handled(self):
        # An odd cycle is exactly the case a greedy matcher gets wrong.
        edges = [(i, (i + 1) % 5, 10 + i) for i in range(5)]
        matching = blossom(edges, maxcardinality=True)
        self.assertEqual(score(matching, edges), exact(5, edges, True))

    def test_result_is_always_a_matching(self):
        random.seed(4)
        for _ in range(200):
            n = random.randint(2, 10)
            pairs = list(itertools.combinations(range(n), 2))
            random.shuffle(pairs)
            edges = [(u, v, random.randint(1, 20)) for u, v in pairs[: random.randint(1, len(pairs))]]
            seen = set()
            for pair in blossom(edges, maxcardinality=True):
                for vertex in pair:
                    self.assertNotIn(vertex, seen)
                    seen.add(vertex)

    def test_matches_the_exact_answer_on_random_graphs(self):
        random.seed(17)
        for trial in range(250):
            n = random.randint(2, 9)
            maxcardinality = trial % 2 == 0
            pairs = list(itertools.combinations(range(n), 2))
            random.shuffle(pairs)
            edges = [(u, v, random.randint(1, 15)) for u, v in pairs[: random.randint(1, len(pairs))]]
            self.assertEqual(
                score(blossom(edges, maxcardinality=maxcardinality), edges)
                if maxcardinality
                else (0, score(blossom(edges, maxcardinality=False), edges)[1]),
                exact(n, edges, maxcardinality),
                msg=f"edges={edges} maxcardinality={maxcardinality}",
            )


class MatcherSelectionTest(unittest.TestCase):
    def test_the_builtin_can_be_asked_for_by_name(self):
        matching, matcher = max_weight_matching([("a", "b", 1)], prefer="builtin")
        self.assertEqual(matcher, "builtin")
        self.assertEqual(matching, {frozenset(("a", "b"))})

    def test_auto_names_whichever_matcher_ran(self):
        _matching, matcher = max_weight_matching([("a", "b", 1)], prefer="auto")
        self.assertIn(matcher, ("networkx", "builtin"))

    def test_both_matchers_agree_when_networkx_is_installed(self):
        try:
            import networkx  # noqa: F401
        except ImportError:
            self.skipTest("networkx is not installed")
        random.seed(5)
        for _ in range(40):
            n = random.randint(2, 12)
            pairs = list(itertools.combinations(range(n), 2))
            random.shuffle(pairs)
            edges = [(u, v, random.randint(1, 30)) for u, v in pairs[: random.randint(1, len(pairs))]]
            from_nx, _ = max_weight_matching(edges, maxcardinality=True, prefer="networkx")
            from_builtin, _ = max_weight_matching(edges, maxcardinality=True, prefer="builtin")
            self.assertEqual(score(from_nx, edges), score(from_builtin, edges))


if __name__ == "__main__":
    unittest.main()
