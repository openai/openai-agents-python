import unittest
from concurrent.futures import ThreadPoolExecutor

from agents import ModelRequest, ModelResponse, ModelRetryAdviceRequest, RetryAdvice
from agents.testing import ModelError, ScriptedModel


def request(identity):
    return ModelRequest({}, {}, "session", "turn", attempt_id=identity)


def advice_request(original, error):
    return ModelRetryAdviceRequest(original, error, original.attempt_id, 1, 1)


class HostAcceptance(unittest.TestCase):
    def test_exact_errors_reverse_advice_and_no_advice_step(self):
        error = RuntimeError("shared")
        model = ScriptedModel(
            [
                ModelError(error, RetryAdvice(reason="first")),
                ModelError(error, RetryAdvice(reason="second")),
                ModelError(error),
            ]
        )
        first, second, third = request("first"), request("second"), request("third")
        for value in (first, second, third):
            with self.assertRaises(RuntimeError) as caught:
                model.complete(value)
            self.assertIs(caught.exception, error)
        model.assert_complete()
        self.assertIsNone(model.get_retry_advice(advice_request(request("second"), error)))
        self.assertIsNone(model.get_retry_advice(advice_request(third, error)))
        self.assertEqual(model.get_retry_advice(advice_request(second, error)).reason, "second")
        self.assertEqual(model.get_retry_advice(advice_request(first, error)).reason, "first")
        self.assertIsNone(model.get_retry_advice(advice_request(first, error)))

    def test_response_capture_and_distinct_exhaustion(self):
        response = ModelResponse([{"type": "message", "text": "captured"}], {"input_tokens": 3})
        model = ScriptedModel([response])
        response.output[0]["text"] = "changed"
        response.usage["input_tokens"] = 9
        actual = model.complete(request("one"))
        self.assertEqual(actual.output[0]["text"], "captured")
        self.assertEqual(actual.usage["input_tokens"], 3)
        model.assert_complete()
        with self.assertRaisesRegex(AssertionError, "Unexpected"):
            model.complete(request("two"))
        with self.assertRaisesRegex(AssertionError, "Unconsumed"):
            ScriptedModel([response]).assert_complete()

    def test_worker_reservation_consumes_each_finite_step_once(self):
        errors = [RuntimeError(str(index)) for index in range(20)]
        model = ScriptedModel([ModelError(error) for error in errors])

        def complete(index):
            try:
                model.complete(request(str(index)))
            except RuntimeError as error:
                return error

        with ThreadPoolExecutor(max_workers=4) as workers:
            actual = list(workers.map(complete, range(20)))
        self.assertEqual({id(error) for error in actual}, {id(error) for error in errors})
        model.assert_complete()


if __name__ == "__main__":
    unittest.main()
