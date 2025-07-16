from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field, ValidationError
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


def test_name_override_without_docstring() -> None:
    """name_override should be used even when not parsing docstrings."""

    def foo(x: int) -> int:
        return x

    fs = function_schema(foo, use_docstring_info=False, name_override="custom")

    assert fs.name == "custom"
    assert fs.params_json_schema.get("title") == "custom_args"


def test_function_with_field_required_constraints():
    """Test function with required Field parameter that has constraints."""

    def func_with_field_constraints(my_number: int = Field(..., gt=10, le=100)) -> int:
        return my_number * 2

    fs = function_schema(func_with_field_constraints, use_docstring_info=False)

    # Check that the schema includes the constraints
    properties = fs.params_json_schema.get("properties", {})
    my_number_schema = properties.get("my_number", {})
    assert my_number_schema.get("type") == "integer"
    assert my_number_schema.get("exclusiveMinimum") == 10  # gt=10
    assert my_number_schema.get("maximum") == 100  # le=100

    # Valid input should work
    valid_input = {"my_number": 50}
    parsed = fs.params_pydantic_model(**valid_input)
    args, kwargs_dict = fs.to_call_args(parsed)
    result = func_with_field_constraints(*args, **kwargs_dict)
    assert result == 100

    # Invalid input: too small (should violate gt=10)
    with pytest.raises(ValidationError):
        fs.params_pydantic_model(**{"my_number": 5})

    # Invalid input: too large (should violate le=100)
    with pytest.raises(ValidationError):
        fs.params_pydantic_model(**{"my_number": 150})


def test_function_with_field_optional_with_default():
    """Test function with optional Field parameter that has default and constraints."""

    def func_with_optional_field(
        required_param: str,
        optional_param: float = Field(default=5.0, ge=0.0),
    ) -> str:
        return f"{required_param}: {optional_param}"

    fs = function_schema(func_with_optional_field, use_docstring_info=False)

    # Check that the schema includes the constraints and description
    properties = fs.params_json_schema.get("properties", {})
    optional_schema = properties.get("optional_param", {})
    assert optional_schema.get("type") == "number"
    assert optional_schema.get("minimum") == 0.0  # ge=0.0
    assert optional_schema.get("default") == 5.0

    # Valid input with default
    valid_input = {"required_param": "test"}
    parsed = fs.params_pydantic_model(**valid_input)
    args, kwargs_dict = fs.to_call_args(parsed)
    result = func_with_optional_field(*args, **kwargs_dict)
    assert result == "test: 5.0"

    # Valid input with explicit value
    valid_input2 = {"required_param": "test", "optional_param": 10.5}
    parsed2 = fs.params_pydantic_model(**valid_input2)
    args2, kwargs_dict2 = fs.to_call_args(parsed2)
    result2 = func_with_optional_field(*args2, **kwargs_dict2)
    assert result2 == "test: 10.5"

    # Invalid input: negative value (should violate ge=0.0)
    with pytest.raises(ValidationError):
        fs.params_pydantic_model(**{"required_param": "test", "optional_param": -1.0})


def test_function_with_field_description_merge():
    """Test that Field descriptions are merged with docstring descriptions."""

    def func_with_field_and_docstring(
        param_with_field_desc: int = Field(..., description="Field description"),
        param_with_both: str = Field(default="hello", description="Field description"),
    ) -> str:
        """
        Function with both field and docstring descriptions.

        Args:
            param_with_field_desc: Docstring description
            param_with_both: Docstring description
        """
        return f"{param_with_field_desc}: {param_with_both}"

    fs = function_schema(func_with_field_and_docstring, use_docstring_info=True)

    # Check that docstring description takes precedence when both exist
    properties = fs.params_json_schema.get("properties", {})
    param1_schema = properties.get("param_with_field_desc", {})
    param2_schema = properties.get("param_with_both", {})

    # The docstring description should be used when both are present
    assert param1_schema.get("description") == "Docstring description"
    assert param2_schema.get("description") == "Docstring description"


def func_with_field_desc_only(
    param_with_field_desc: int = Field(..., description="Field description only"),
    param_without_desc: str = Field(default="hello"),
) -> str:
    return f"{param_with_field_desc}: {param_without_desc}"


def test_function_with_field_description_only():
    """Test that Field descriptions are used when no docstring info."""

    fs = function_schema(func_with_field_desc_only)

    # Check that field description is used when no docstring
    properties = fs.params_json_schema.get("properties", {})
    param1_schema = properties.get("param_with_field_desc", {})
    param2_schema = properties.get("param_without_desc", {})

    assert param1_schema.get("description") == "Field description only"
    assert param2_schema.get("description") is None


def test_function_with_field_string_constraints():
    """Test function with Field parameter that has string-specific constraints."""

    def func_with_string_field(
        name: str = Field(..., min_length=3, max_length=20, pattern=r"^[A-Za-z]+$"),
    ) -> str:
        return f"Hello, {name}!"

    fs = function_schema(func_with_string_field, use_docstring_info=False)

    # Check that the schema includes string constraints
    properties = fs.params_json_schema.get("properties", {})
    name_schema = properties.get("name", {})
    assert name_schema.get("type") == "string"
    assert name_schema.get("minLength") == 3
    assert name_schema.get("maxLength") == 20
    assert name_schema.get("pattern") == r"^[A-Za-z]+$"

    # Valid input
    valid_input = {"name": "Alice"}
    parsed = fs.params_pydantic_model(**valid_input)
    args, kwargs_dict = fs.to_call_args(parsed)
    result = func_with_string_field(*args, **kwargs_dict)
    assert result == "Hello, Alice!"

    # Invalid input: too short
    with pytest.raises(ValidationError):
        fs.params_pydantic_model(**{"name": "Al"})

    # Invalid input: too long
    with pytest.raises(ValidationError):
        fs.params_pydantic_model(**{"name": "A" * 25})

    # Invalid input: doesn't match pattern (contains numbers)
    with pytest.raises(ValidationError):
        fs.params_pydantic_model(**{"name": "Alice123"})


def test_function_with_field_multiple_constraints():
    """Test function with multiple Field parameters having different constraint types."""

    def func_with_multiple_field_constraints(
        score: int = Field(..., ge=0, le=100, description="Score from 0 to 100"),
        name: str = Field(default="Unknown", min_length=1, max_length=50),
        factor: float = Field(default=1.0, gt=0.0, description="Positive multiplier"),
    ) -> str:
        final_score = score * factor
        return f"{name} scored {final_score}"

    fs = function_schema(func_with_multiple_field_constraints, use_docstring_info=False)

    # Check schema structure
    properties = fs.params_json_schema.get("properties", {})

    # Check score field
    score_schema = properties.get("score", {})
    assert score_schema.get("type") == "integer"
    assert score_schema.get("minimum") == 0
    assert score_schema.get("maximum") == 100
    assert score_schema.get("description") == "Score from 0 to 100"

    # Check name field
    name_schema = properties.get("name", {})
    assert name_schema.get("type") == "string"
    assert name_schema.get("minLength") == 1
    assert name_schema.get("maxLength") == 50
    assert name_schema.get("default") == "Unknown"

    # Check factor field
    factor_schema = properties.get("factor", {})
    assert factor_schema.get("type") == "number"
    assert factor_schema.get("exclusiveMinimum") == 0.0
    assert factor_schema.get("default") == 1.0
    assert factor_schema.get("description") == "Positive multiplier"

    # Valid input with defaults
    valid_input = {"score": 85}
    parsed = fs.params_pydantic_model(**valid_input)
    args, kwargs_dict = fs.to_call_args(parsed)
    result = func_with_multiple_field_constraints(*args, **kwargs_dict)
    assert result == "Unknown scored 85.0"

    # Valid input with all parameters
    valid_input2 = {"score": 90, "name": "Alice", "factor": 1.5}
    parsed2 = fs.params_pydantic_model(**valid_input2)
    args2, kwargs_dict2 = fs.to_call_args(parsed2)
    result2 = func_with_multiple_field_constraints(*args2, **kwargs_dict2)
    assert result2 == "Alice scored 135.0"

    # Test various validation errors
    with pytest.raises(ValidationError):  # score too high
        fs.params_pydantic_model(**{"score": 150})

    with pytest.raises(ValidationError):  # empty name
        fs.params_pydantic_model(**{"score": 50, "name": ""})

    with pytest.raises(ValidationError):  # zero factor
        fs.params_pydantic_model(**{"score": 50, "factor": 0.0})
