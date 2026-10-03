"""Exact provider price arithmetic under caller-controlled Decimal contexts."""

from decimal import Decimal, Inexact, Overflow, Rounded, Underflow, localcontext
from fractions import Fraction
import random
from types import SimpleNamespace
import unittest

from dao.ledger import Ledger
from dao.providers import OpenAIProvider
from dao.store import Store


class ProviderMathTests(unittest.TestCase):
    @staticmethod
    def provider(input_tokens, output_tokens, cached_tokens, rates):
        response = SimpleNamespace(
            id="mock-price-response",
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
            ),
            output=[],
            output_text="mock reply",
        )
        # Exercise the real response arithmetic without importing the optional SDK.
        provider = object.__new__(OpenAIProvider)
        provider.model = "mock-model"
        provider.max_output_tokens = 100
        provider.input_rate, provider.output_rate, provider.cached_rate = map(Decimal, rates)
        provider.client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: response)
        )
        return provider

    def test_cost_and_ledger_rounding_preserve_digits_at_low_precision(self):
        for precision in (2, 6, 28):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                context.Emax = 5
                context.Emin = -5
                for signal in (Inexact, Rounded, Overflow, Underflow):
                    context.traps[signal] = True
                reply = self.provider(11, 0, 0, ("1.50", "0", "0")).respond([], [])
                self.assertEqual(reply.cost_usd, "0.0000165")
                self.assertEqual(reply.metadata["input_rate"], "1.50")
                with Store(":memory:") as store:
                    ledger = Ledger(store, "1")
                    ledger.reserve("mock-call", "main", "0.1")
                    entry = ledger.settle(
                        "mock-call", reply.input_tokens, reply.output_tokens,
                        reply.cost_usd, reply.model,
                    )
                    self.assertEqual(entry["cost_usd"], "0.000017")

    def test_cached_input_cost_is_exact_and_does_not_double_count(self):
        cases = [
            (11, 7, 3, ("1.50", "6.00", "0.75")),
            (11, 7, 11, ("1.50", "6.00", "0.75")),
            (11, 7, 0, ("1.50", "6.00", "0.75")),
            (0, 0, 0, ("1.50", "6.00", "0.75")),
            (1250, 0, 0, ("2", "0", "0")),
        ]
        for inputs, outputs, cached, rates in cases:
            with self.subTest(inputs=inputs, outputs=outputs, cached=cached), localcontext() as context:
                context.prec = 2
                reply = self.provider(inputs, outputs, cached, rates).respond([], [])
                ir, outr, cr = map(Fraction, rates)
                expected = ((inputs - cached) * ir + cached * cr + outputs * outr) / 1_000_000
                self.assertEqual(Fraction(reply.cost_usd), expected)
                self.assertEqual(reply.metadata["cached_input_tokens"], cached)
        reply = self.provider(1250, 0, 0, ("2", "0", "0")).respond([], [])
        self.assertEqual(reply.cost_usd, "0.0025")

    def test_default_precision_and_long_rate_tail_remain_exact(self):
        for tail in (26, 5000):
            rate = "1." + "0" * tail + "1"
            with self.subTest(tail=tail), localcontext() as context:
                context.prec = 28
                reply = self.provider(1, 0, 0, (rate, "0", "0")).respond([], [])
                self.assertEqual(reply.cost_usd, "0.000001" + "0" * tail + "1")
                with Store(":memory:") as store:
                    ledger = Ledger(store, "1")
                    ledger.reserve("long-rate", "main", "0.1")
                    entry = ledger.settle("long-rate", 1, 0, reply.cost_usd, reply.model)
                    self.assertEqual(entry["cost_usd"], "0.000002")

    def test_unused_zero_and_extreme_exponents_do_not_affect_cost(self):
        cases = [
            (("1e-10000000", "0e10000000", "0"), "1E-10000006"),
            (("1.50", "1e-10000000", "1e10000000"), "0.0000015"),
            (("1e15", "0", "0"), "1E+9"),
        ]
        for rates, expected in cases:
            with self.subTest(rates=rates), localcontext() as context:
                context.prec = 2
                context.Emax = 5
                context.Emin = -5
                reply = self.provider(1, 0, 0, rates).respond([], [])
                self.assertEqual(reply.cost_usd, expected)

    def test_randomized_price_expression_matches_fraction_reference(self):
        rng = random.Random(20261001)
        with localcontext() as context:
            context.prec = 2
            context.Emax = 5
            context.Emin = -5
            for _ in range(200):
                inputs = rng.randrange(0, 1_000_000)
                outputs = rng.randrange(0, 100_000)
                cached = rng.randrange(0, inputs + 1)
                rates = tuple(f"{rng.randrange(0, 100_000_000)}e-{rng.randrange(0, 20)}" for _ in range(3))
                ir, outr, cr = map(Fraction, rates)
                expected = ((inputs - cached) * ir + cached * cr + outputs * outr) / 1_000_000
                reply = self.provider(inputs, outputs, cached, rates).respond([], [])
                self.assertEqual(Fraction(reply.cost_usd), expected)


if __name__ == "__main__":
    unittest.main()
