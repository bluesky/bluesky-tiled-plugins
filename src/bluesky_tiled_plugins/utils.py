import collections.abc


def truncate_json_overflow(data):
    """Truncate large numerical values to avoid overflow issues when serializing as JSON.

    This preemptively truncates large integers and floats with zero fractional part to fit within
    the JSON limits for integers, i.e. (-2^53, 2^53 - 1], in case the values are implicitly
    converted during serialization.
    """
    if isinstance(data, collections.abc.Mapping):
        return {k: truncate_json_overflow(v) for k, v in data.items()}
    if isinstance(data, collections.abc.Iterable) and not isinstance(data, str):
        # Handle lists, tuples, arrays, etc., but not strings
        return [truncate_json_overflow(item) for item in data]
    if (
        isinstance(data, (int, float))
        and not (data % 1)
        and not (1 - 2**53 <= data <= 2**53 - 1)
    ):
        return min(
            max(data, 1 - 2**53), 2**53 - 1
        )  # Truncate integers to fit in JSON (53 bits max)
    if isinstance(data, float) and (data < -1.7976e308 or data > 1.7976e308):
        return min(
            max(data, -1.7976e308), 1.7976e308
        )  # (Approx.) truncate floats to fit in JSON to avoid inf
    return data


def list_summands(A: int, b: int, repeat: int = 1) -> tuple[int, ...]:
    # Generate a list with repeated b summing up to A; append the remainder if necessary
    # e.g. list_summands(13, 3) = [3, 3, 3, 3, 1]
    # if `repeat = n`, n > 1, copy and repeat the entire result n times
    return tuple([b] * (A // b) + ([A % b] if A % b > 0 else [])) * repeat or (0,)
