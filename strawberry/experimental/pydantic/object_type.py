import builtins
import dataclasses
import warnings
from functools import partial
from typing import Any, Dict, List, Optional, Sequence, Type, cast

from pydantic import BaseModel
from pydantic.fields import ModelField
from typing_extensions import Literal

from graphql import GraphQLResolveInfo

import strawberry
from strawberry.arguments import UNSET
from strawberry.experimental.pydantic.conversion import (
    convert_pydantic_model_to_strawberry_class,
)
from strawberry.experimental.pydantic.fields import get_basic_type
from strawberry.experimental.pydantic.utils import (
    DataclassCreationFields,
    ensure_all_auto_fields_in_pydantic,
    get_default_factory_for_field,
    get_private_fields,
    sort_creation_fields,
)
from strawberry.field import StrawberryField
from strawberry.object_type import _process_type, _wrap_dataclass
from strawberry.schema_directive import StrawberrySchemaDirective
from strawberry.types.type_resolver import _get_fields
from strawberry.types.types import TypeDefinition

from .exceptions import MissingFieldsListError, UnregisteredTypeException


def replace_pydantic_types(type_: Any):
    origin = getattr(type_, "__origin__", None)
    if origin is Literal:
        # Literal does not have types in its __args__ so we return early
        return type_
    if hasattr(type_, "__args__"):
        replaced_type = type_.copy_with(
            tuple(replace_pydantic_types(t) for t in type_.__args__)
        )

        if isinstance(replaced_type, TypeDefinition):
            # TODO: Not sure if this is necessary. No coverage in tests
            # TODO: Unnecessary with StrawberryObject

            replaced_type = builtins.type(
                replaced_type.name,
                (),
                {"_type_definition": replaced_type},
            )

        return replaced_type

    if issubclass(type_, BaseModel):
        if hasattr(type_, "_strawberry_type"):
            return type_._strawberry_type
        else:
            raise UnregisteredTypeException(type_)

    return type_


def get_type_for_field(field: ModelField):
    type_ = field.outer_type_
    type_ = get_basic_type(type_)
    type_ = replace_pydantic_types(type_)

    if not field.required:
        type_ = Optional[type_]

    return type_


def type(
    model: Type[BaseModel],
    *,
    fields: Optional[List[str]] = None,
    name: Optional[str] = None,
    is_input: bool = False,
    is_interface: bool = False,
    description: Optional[str] = None,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    all_fields: bool = False,
):
    def wrap(cls):
        model_fields = model.__fields__
        fields_set = set(fields) if fields else set([])

        if fields:
            warnings.warn(
                "`fields` is deprecated, use `auto` type annotations instead",
                DeprecationWarning,
            )

        existing_fields = getattr(cls, "__annotations__", {})
        fields_set = fields_set.union(
            set(name for name, typ in existing_fields.items() if typ is strawberry.auto)
        )

        if all_fields:
            if fields_set:
                warnings.warn(
                    "Using all_fields overrides any explicitly defined fields "
                    "in the model, using both is likely a bug",
                    stacklevel=2,
                )
            fields_set = set(model_fields.keys())

        if not fields_set:
            raise MissingFieldsListError(cls)

        ensure_all_auto_fields_in_pydantic(
            model=model, auto_fields=fields_set, cls_name=cls.__name__
        )

        all_model_fields: List[DataclassCreationFields] = [
            DataclassCreationFields(
                name=field_name,
                type_annotation=get_type_for_field(field),
                field=StrawberryField(
                    python_name=field.name,
                    graphql_name=field.alias if field.has_alias else None,
                    # always unset because we use default_factory instead
                    default=UNSET,
                    default_factory=get_default_factory_for_field(field),
                    type_annotation=get_type_for_field(field),
                    description=field.field_info.description,
                ),
            )
            for field_name, field in model_fields.items()
            if field_name in fields_set
        ]

        wrapped = _wrap_dataclass(cls)
        extra_fields = cast(List[dataclasses.Field], _get_fields(wrapped))
        private_fields = get_private_fields(wrapped)

        all_model_fields.extend(
            (
                DataclassCreationFields(
                    name=field.name,
                    type_annotation=field.type,
                    field=field,
                )
                for field in extra_fields + private_fields
                if field.type != strawberry.auto
            )
        )

        # Sort fields so that fields with missing defaults go first
        sorted_fields = sort_creation_fields(all_model_fields)

        # Implicitly define `is_type_of` to support interfaces/unions that use
        # pydantic objects (not the corresponding strawberry type)
        @classmethod  # type: ignore
        def is_type_of(cls: Type, obj: Any, _info: GraphQLResolveInfo) -> bool:
            return isinstance(obj, (cls, model))

        cls = dataclasses.make_dataclass(
            cls.__name__,
            [field.to_tuple() for field in sorted_fields],
            bases=cls.__bases__,
            namespace={"is_type_of": is_type_of},
        )

        _process_type(
            cls,
            name=name,
            is_input=is_input,
            is_interface=is_interface,
            description=description,
            directives=directives,
        )

        model._strawberry_type = cls  # type: ignore
        cls._pydantic_type = model  # type: ignore

        def from_pydantic(instance: Any, extra: Dict[str, Any] = None) -> Any:
            return convert_pydantic_model_to_strawberry_class(
                cls=cls, model_instance=instance, extra=extra
            )

        def to_pydantic(self) -> Any:
            instance_kwargs = dataclasses.asdict(self)

            return model(**instance_kwargs)

        cls.from_pydantic = staticmethod(from_pydantic)
        cls.to_pydantic = to_pydantic

        return cls

    return wrap


input = partial(type, is_input=True)

interface = partial(type, is_interface=True)
