import collections.abc
import re


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


def compile_template(template: str, filename: str = "") -> str:
    """Compile a filename template from old-style to new-style Python formatting

    Parameters
    ----------
    template : str
        An old-style Python formatting string, e.g. "%s%s_%06d.tif
    filename : str
        An optional filename to substitute for the first %s in the template.

    Returns
    -------
        A new-style Python formatting string, e.g. "filename_{:06d}.tif"
    """

    def int_replacer(match):
        """Normalize filename template

        Replace an integer format specifier with a new-style format specifier,
        i.e. convert the template string from "old" to "new" Python style,
        e.g. "%s%s_%06d.tif" to "filename_{:06d}.tif"

        """
        flags, width, precision, type_char = match.groups()

        # Handle the flags
        flag_str = ""
        if "-" in flags:
            flag_str = "<"  # Left-align
        if "+" in flags:
            flag_str += "+"  # Show positive sign
        elif " " in flags:
            flag_str += " "  # Space before positive numbers
        if "0" in flags:
            flag_str += "0"  # Zero padding

        # Build width and precision if they exist
        width_str = width if width else ""
        precision_str = f".{precision}" if precision else ""

        # Handle cases like "%6.6d", which should be converted to "{:06d}"
        if precision and width:
            flag_str = "0"
            precision_str = ""
            width_str = str(max(precision, width))

        # Construct the new-style format specifier
        return f"{{:{flag_str}{width_str}{precision_str}{type_char}}}"

    result = (
        template.replace("%s", "{:s}", 1).replace("%s", "").replace("{:s}", filename, 1)
    )
    result = re.sub(r"%([-+#0 ]*)(\d+)?(?:\.(\d+))?([d])", int_replacer, result)

    return result


def list_summands(A: int, b: int, repeat: int = 1) -> tuple[int, ...]:
    # Generate a list with repeated b summing up to A; append the remainder if necessary
    # e.g. list_summands(13, 3) = [3, 3, 3, 3, 1]
    # if `repeat = n`, n > 1, copy and repeat the entire result n times
    return tuple([b] * (A // b) + ([A % b] if A % b > 0 else [])) * repeat or (0,)
