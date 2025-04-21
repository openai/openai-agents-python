from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError
from typing_extensions import TypedDict

from agents import RunContextWrapper
from agents.exceptions import UserError
from agents.function_schema import function_schema


def no_args_function():
    """This function has no args."""

    return "ok"


def test_no_args_function():
    func_schema = function_schema(no_args_function)
    assert func_schema.params_json_schema.get("title") == "no_args_function_args"
    assert func_schema.description == "This function has no args."
    assert not func_schema.takes_context

    parsed = func_schema.params_pydantic_model()
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = no_args_function(*args, **kwargs_dict)
    assert result == "ok"


def no_args_function_with_context(ctx: RunContextWrapper[str]):
    return "ok"


def test_no_args_function_with_context() -> None:
    func_schema = function_schema(no_args_function_with_context)
    assert func_schema.takes_context

    context = RunContextWrapper(context="test")
    parsed = func_schema.params_pydantic_model()
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = no_args_function_with_context(context, *args, **kwargs_dict)
    assert result == "ok"


def simple_function(a: int, b: int = 5):
    """
    Args:
        a: The first argument
        b: The second argument

    Returns:
        The sum of a and b
    """
    return a + b


def test_simple_function():
    """Test a function that has simple typed parameters and defaults."""

    func_schema = function_schema(simple_function)
    # Check that the JSON schema is a dictionary with title, type, etc.
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "simple_function_args"
    assert (
        func_schema.params_json_schema.get("properties", {}).get("a").get("description")
        == "The first argument"
    )
    assert (
        func_schema.params_json_schema.get("properties", {}).get("b").get("description")
        == "The second argument"
    )
    assert not func_schema.takes_context

    # Valid input
    valid_input = {"a": 3}
    parsed = func_schema.params_pydantic_model(**valid_input)
    args_tuple, kwargs_dict = func_schema.to_call_args(parsed)
    result = simple_function(*args_tuple, **kwargs_dict)
    assert result == 8  # 3 + 5

    # Another valid input
    valid_input2 = {"a": 3, "b": 10}
    parsed2 = func_schema.params_pydantic_model(**valid_input2)
    args_tuple2, kwargs_dict2 = func_schema.to_call_args(parsed2)
    result2 = simple_function(*args_tuple2, **kwargs_dict2)
    assert result2 == 13  # 3 + 10

    # Invalid input: 'a' must be int
    with pytest.raises(ValidationError):
        func_schema.params_pydantic_model(**{"a": "not an integer"})


def varargs_function(x: int, *numbers: float, flag: bool = False, **kwargs: Any):
    return x, numbers, flag, kwargs


def test_varargs_function():
    """Test a function that uses *args and **kwargs."""

    func_schema = function_schema(varargs_function, strict_json_schema=False)
    # Check JSON schema structure
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "varargs_function_args"

    # Valid input including *args in 'numbers' and **kwargs in 'kwargs'
    valid_input = {
        "x": 10,
        "numbers": [1.1, 2.2, 3.3],
        "flag": True,
        "kwargs": {"extra1": "hello", "extra2": 42},
    }
    parsed = func_schema.params_pydantic_model(**valid_input)
    args, kwargs_dict = func_schema.to_call_args(parsed)

    result = varargs_function(*args, **kwargs_dict)
    # result should be (10, (1.1, 2.2, 3.3), True, {"extra1": "hello", "extra2": 42})
    assert result[0] == 10
    assert result[1] == (1.1, 2.2, 3.3)
    assert result[2] is True
    assert result[3] == {"extra1": "hello", "extra2": 42}

    # Missing 'x' should raise error
    with pytest.raises(ValidationError):
        func_schema.params_pydantic_model(**{"numbers": [1.1, 2.2]})

    # 'flag' can be omitted because it has a default
    valid_input_no_flag = {"x": 7, "numbers": [9.9], "kwargs": {"some_key": "some_value"}}
    parsed2 = func_schema.params_pydantic_model(**valid_input_no_flag)
    args2, kwargs_dict2 = func_schema.to_call_args(parsed2)
    result2 = varargs_function(*args2, **kwargs_dict2)
    # result2 should be (7, (9.9,), False, {'some_key': 'some_value'})
    assert result2 == (7, (9.9,), False, {"some_key": "some_value"})


class Foo(TypedDict):
    a: int
    b: str


class InnerModel(BaseModel):
    a: int
    b: str


class OuterModel(BaseModel):
    inner: InnerModel
    foo: Foo


def complex_args_function(model: OuterModel) -> str:
    return f"{model.inner.a}, {model.inner.b}, {model.foo['a']}, {model.foo['b']}"


def test_nested_data_function():
    func_schema = function_schema(complex_args_function)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "complex_args_function_args"

    # Valid input
    model = OuterModel(inner=InnerModel(a=1, b="hello"), foo=Foo(a=2, b="world"))
    valid_input = {
        "model": model.model_dump(),
    }

    parsed = func_schema.params_pydantic_model(**valid_input)
    args, kwargs_dict = func_schema.to_call_args(parsed)

    result = complex_args_function(*args, **kwargs_dict)
    assert result == "1, hello, 2, world"


def complex_args_and_docs_function(model: OuterModel, some_flag: int = 0) -> str:
    """
    This function takes a model and a flag, and returns a string.

    Args:
        model: A model with an inner and foo field
        some_flag: An optional flag with a default of 0

    Returns:
        A string with the values of the model and flag
    """
    return f"{model.inner.a}, {model.inner.b}, {model.foo['a']}, {model.foo['b']}, {some_flag or 0}"


def test_complex_args_and_docs_function():
    func_schema = function_schema(complex_args_and_docs_function)

    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "complex_args_and_docs_function_args"

    # Check docstring is parsed correctly
    properties = func_schema.params_json_schema.get("properties", {})
    assert properties.get("model").get("description") == "A model with an inner and foo field"
    assert properties.get("some_flag").get("description") == "An optional flag with a default of 0"

    # Valid input
    model = OuterModel(inner=InnerModel(a=1, b="hello"), foo=Foo(a=2, b="world"))
    valid_input = {
        "model": model.model_dump(),
    }

    parsed = func_schema.params_pydantic_model(**valid_input)
    args, kwargs_dict = func_schema.to_call_args(parsed)

    result = complex_args_and_docs_function(*args, **kwargs_dict)
    assert result == "1, hello, 2, world, 0"

    # Invalid input: 'some_flag' must be int
    with pytest.raises(ValidationError):
        func_schema.params_pydantic_model(
            **{"model": model.model_dump(), "some_flag": "not an int"}
        )

    # Valid input: 'some_flag' can be omitted because it has a default
    valid_input_no_flag = {"model": model.model_dump()}
    parsed2 = func_schema.params_pydantic_model(**valid_input_no_flag)
    args2, kwargs_dict2 = func_schema.to_call_args(parsed2)
    result2 = complex_args_and_docs_function(*args2, **kwargs_dict2)
    assert result2 == "1, hello, 2, world, 0"


def function_with_context(ctx: RunContextWrapper[str], a: int, b: int = 5):
    return a + b


def test_function_with_context():
    func_schema = function_schema(function_with_context)
    assert func_schema.takes_context

    context = RunContextWrapper(context="test")

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)

    result = function_with_context(context, *args, **kwargs_dict)
    assert result == 3


class MyClass:
    def foo(self, a: int, b: int = 5):
        return a + b

    def foo_ctx(self, ctx: RunContextWrapper[str], a: int, b: int = 5):
        return a + b

    @classmethod
    def bar(cls, a: int, b: int = 5):
        return a + b

    @classmethod
    def bar_ctx(cls, ctx: RunContextWrapper[str], a: int, b: int = 5):
        return a + b

    @staticmethod
    def baz(a: int, b: int = 5):
        return a + b

    @staticmethod
    def baz_ctx(ctx: RunContextWrapper[str], a: int, b: int = 5):
        return a + b


def test_class_based_functions():
    context = RunContextWrapper(context="test")

    # Instance method
    instance = MyClass()
    func_schema = function_schema(instance.foo)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "foo_args"

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = instance.foo(*args, **kwargs_dict)
    assert result == 3

    # Instance method with context
    func_schema = function_schema(instance.foo_ctx)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "foo_ctx_args"
    assert func_schema.takes_context

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = instance.foo_ctx(context, *args, **kwargs_dict)
    assert result == 3

    # Class method
    func_schema = function_schema(MyClass.bar)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "bar_args"

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = MyClass.bar(*args, **kwargs_dict)
    assert result == 3

    # Class method with context
    func_schema = function_schema(MyClass.bar_ctx)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "bar_ctx_args"
    assert func_schema.takes_context

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = MyClass.bar_ctx(context, *args, **kwargs_dict)
    assert result == 3

    # Static method
    func_schema = function_schema(MyClass.baz)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "baz_args"

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = MyClass.baz(*args, **kwargs_dict)
    assert result == 3

    # Static method with context
    func_schema = function_schema(MyClass.baz_ctx)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "baz_ctx_args"
    assert func_schema.takes_context

    input = {"a": 1, "b": 2}
    parsed = func_schema.params_pydantic_model(**input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = MyClass.baz_ctx(context, *args, **kwargs_dict)
    assert result == 3


class MyEnum(str, Enum):
    FOO = "foo"
    BAR = "bar"
    BAZ = "baz"


def enum_and_literal_function(a: MyEnum, b: Literal["a", "b", "c"]) -> str:
    return f"{a.value} {b}"


def test_enum_and_literal_function():
    func_schema = function_schema(enum_and_literal_function)
    assert isinstance(func_schema.params_json_schema, dict)
    assert func_schema.params_json_schema.get("title") == "enum_and_literal_function_args"

    # Check that the enum values are included in the JSON schema
    assert func_schema.params_json_schema.get("$defs", {}).get("MyEnum", {}).get("enum") == [
        "foo",
        "bar",
        "baz",
    ]

    # Check that the enum is expressed as a def
    assert (
        func_schema.params_json_schema.get("properties", {}).get("a", {}).get("$ref")
        == "#/$defs/MyEnum"
    )

    # Check that the literal values are included in the JSON schema
    assert func_schema.params_json_schema.get("properties", {}).get("b", {}).get("enum") == [
        "a",
        "b",
        "c",
    ]

    # Valid input
    valid_input = {"a": "foo", "b": "a"}
    parsed = func_schema.params_pydantic_model(**valid_input)
    args, kwargs_dict = func_schema.to_call_args(parsed)
    result = enum_and_literal_function(*args, **kwargs_dict)
    assert result == "foo a"

    # Invalid input: 'a' must be a valid enum value
    with pytest.raises(ValidationError):
        func_schema.params_pydantic_model(**{"a": "not an enum value", "b": "a"})

    # Invalid input: 'b' must be a valid literal value
    with pytest.raises(ValidationError):
        func_schema.params_pydantic_model(**{"a": "foo", "b": "not a literal value"})


def test_run_context_in_non_first_position_raises_value_error():
    # When a parameter (after the first) is annotated as RunContextWrapper,
    # function_schema() should raise a UserError.
    def func(a: int, context: RunContextWrapper) -> None:
        pass

    with pytest.raises(UserError):
        function_schema(func, use_docstring_info=False)


def test_var_positional_tuple_annotation():
    # When a function has a var-positional parameter annotated with a tuple type,
    # function_schema() should convert it into a field with type List[<tuple-element>].
    def func(*args: tuple[int, ...]) -> int:
        total = 0
        for arg in args:
            total += sum(arg)
        return total

    fs = function_schema(func, use_docstring_info=False)

    properties = fs.params_json_schema.get("properties", {})
    assert properties.get("args").get("type") == "array"
    assert properties.get("args").get("items").get("type") == "integer"


def test_var_keyword_dict_annotation():
    # Case 3:
    # When a function has a var-keyword parameter annotated with a dict type,
    # function_schema() should convert it into a field with type Dict[<key>, <value>].
    def func(**kwargs: dict[str, int]):
        return kwargs

    fs = function_schema(func, use_docstring_info=False, strict_json_schema=False)

    properties = fs.params_json_schema.get("properties", {})
    # The name of the field is "kwargs", and it's a JSON object i.e. a dict.
    assert properties.get("kwargs").get("type") == "object"
    # The values in the dict are integers.
    assert properties.get("kwargs").get("additionalProperties").get("type") == "integer"


def test_schema_with_mapping_raises_strict_mode_error():
    """A mapping type is not allowed in strict mode. Same for dicts. Ensure we raise a UserError."""

    def func_with_mapping(test_one: Mapping[str, int]) -> str:
        return "foo"

    with pytest.raises(UserError):
        function_schema(func_with_mapping)
