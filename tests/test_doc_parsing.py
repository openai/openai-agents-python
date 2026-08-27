from agents.function_schema import generate_func_documentation


def func_foo_google(a: int, b: float) -> str:
    """
    This is func_foo.

    Args:
        a: The first argument.
        b: The second argument.

    Returns:
        A result
    """

    return "ok"


def func_foo_numpy(a: int, b: float) -> str:
    """
    This is func_foo.

    Parameters
    ----------
    a: int
        The first argument.
    b: float
        The second argument.

    Returns
    -------
    str
        A result
    """
    return "ok"


def func_foo_sphinx(a: int, b: float) -> str:
    """
    This is func_foo.

    :param a: The first argument.
    :param b: The second argument.
    :return: A result
    """
    return "ok"


class Bar:
    def func_bar(self, a: int, b: float) -> str:
        """
        This is func_bar.

        Args:
            a: The first argument.
            b: The second argument.

        Returns:
            A result
        """
        return "ok"

    @classmethod
    def func_baz(cls, a: int, b: float) -> str:
        """
        This is func_baz.

        Args:
            a: The first argument.
            b: The second argument.

        Returns:
            A result
        """
        return "ok"


def test_functions_are_ok():
    func_foo_google(1, 2.0)
    func_foo_numpy(1, 2.0)
    func_foo_sphinx(1, 2.0)
    Bar().func_bar(1, 2.0)
    Bar.func_baz(1, 2.0)


def test_auto_detection() -> None:
    doc = generate_func_documentation(func_foo_google)
    assert doc.name == "func_foo_google"
    assert doc.description == "This is func_foo."
    assert doc.param_descriptions == {"a": "The first argument.", "b": "The second argument."}

    doc = generate_func_documentation(func_foo_numpy)
    assert doc.name == "func_foo_numpy"
    assert doc.description == "This is func_foo."
    assert doc.param_descriptions == {"a": "The first argument.", "b": "The second argument."}

    doc = generate_func_documentation(func_foo_sphinx)
    assert doc.name == "func_foo_sphinx"
    assert doc.description == "This is func_foo."
    assert doc.param_descriptions == {"a": "The first argument.", "b": "The second argument."}


def test_instance_method() -> None:
    bar = Bar()
    doc = generate_func_documentation(bar.func_bar)
    assert doc.name == "func_bar"
    assert doc.description == "This is func_bar."
    assert doc.param_descriptions == {"a": "The first argument.", "b": "The second argument."}


def test_classmethod() -> None:
    doc = generate_func_documentation(Bar.func_baz)
    assert doc.name == "func_baz"
    assert doc.description == "This is func_baz."
    assert doc.param_descriptions == {"a": "The first argument.", "b": "The second argument."}
