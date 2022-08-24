import functools
from datetime import datetime, timezone
from typing import Dict

from marshmallow import (
    EXCLUDE,
    INCLUDE,
    Schema,
    ValidationError,
    fields,
    pre_load,
    validate,
    validates_schema,
)
from structlog import get_logger

from app.questionnaire.rules.utils import parse_iso_8601_datetime

logger = get_logger()


class RegionCode(validate.Regexp):
    """A region code defined as per ISO 3166-2:GB
    Currently, this does not validate the subdivision, but only checks length
    """

    def __init__(self, *args, **kwargs):
        super().__init__("^GB-[A-Z]{3}$", *args, **kwargs)


class UUIDString(fields.UUID):
    """Currently, runner cannot handle UUID objects in metadata
    Since all metadata is serialized and deserialized to JSON.
    This custom field deserializes UUIDs to strings.
    """

    def _deserialize(self, *args, **kwargs):  # pylint: disable=arguments-differ
        return str(super()._deserialize(*args, **kwargs))


class DateString(fields.DateTime):
    """Currently, runner cannot handle Date objects in metadata
    Since all metadata is serialized and deserialized to JSON.
    This custom field deserializes Dates to strings.
    """

    def _deserialize(self, *args, **kwargs):  # pylint: disable=arguments-differ
        date = super()._deserialize(*args, **kwargs)

        if self.format == "iso8601":
            return date.isoformat()

        return date.strftime(self.format)


VALIDATORS = {
    "date": functools.partial(DateString, format="%Y-%m-%d", required=True),
    "uuid": functools.partial(UUIDString, required=True),
    "boolean": functools.partial(fields.Boolean, required=True),
    "string": functools.partial(fields.String, required=True),
    "url": functools.partial(fields.Url, required=True),
    "iso_8601_date_string": functools.partial(
        DateString, format="iso8601", required=True
    ),
}


class StripWhitespaceMixin:
    @pre_load()
    def strip_whitespace(
        self, items, **kwargs
    ):  # pylint: disable=no-self-use, unused-argument
        for key, value in items.items():
            if isinstance(value, str):
                items[key] = value.strip()
        return items


class Data(Schema, StripWhitespaceMixin):
    pass


class SurveyMetadata(Schema, StripWhitespaceMixin):
    data = fields.Nested(Data, unknown=INCLUDE)
    receipting_keys = fields.List(fields.String)


class RunnerMetadataSchema(Schema, StripWhitespaceMixin):
    """Metadata which is required for the operation of runner itself"""

    jti = VALIDATORS["uuid"]()  # type:ignore
    tx_id = VALIDATORS["uuid"]()  # type:ignore
    case_id = VALIDATORS["uuid"]()  # type:ignore
    collection_exercise_sid = VALIDATORS["string"](
        validate=validate.Length(min=1)
    )  # type:ignore
    version = VALIDATORS["string"](required=True)  # type:ignore
    schema_name = VALIDATORS["string"](required=False)  # type:ignore
    schema_url = VALIDATORS["url"](required=False)  # type:ignore
    response_id = VALIDATORS["string"](required=True)  # type:ignore
    account_service_url = VALIDATORS["url"](required=True)  # type:ignore

    language_code = VALIDATORS["string"](required=False)  # type:ignore
    channel = VALIDATORS["string"](
        required=False, validate=validate.Length(min=1)
    )  # type:ignore
    response_expires_at = VALIDATORS["iso_8601_date_string"](
        required=False,
        validate=lambda x: parse_iso_8601_datetime(x) > datetime.now(tz=timezone.utc),
    )  # type:ignore
    region_code = VALIDATORS["string"](
        required=False, validate=RegionCode()
    )  # type:ignore

    roles = fields.List(fields.String(), required=False)
    survey_metadata = fields.Nested(SurveyMetadata)

    @validates_schema
    def validate_receipting_keys(self, data, **kwargs):
        # pylint: disable=no-self-use, unused-argument
        if data and (
            receipting_keys := data.get("survey_metadata").get("receipting_keys")
        ):
            for receipting_key in receipting_keys:
                if receipting_key not in data.get("survey_metadata").get("data"):
                    raise ValidationError(
                        "Receipting key(s) not set in Survey Metadata"
                    )

    @validates_schema
    def validate_schema_name_is_set(self, data, **kwargs):
        # pylint: disable=no-self-use, unused-argument
        if data and not data.get("schema_name") or data.get("schema_url"):
            raise ValidationError(
                "Neither schema_name or schema_url has been set in metadata"
            )


def validate_questionnaire_claims_v2(claims, questionnaire_specific_metadata):
    """Validate any survey specific claims required for a questionnaire"""
    dynamic_fields = {}

    for metadata_field in questionnaire_specific_metadata:
        field_arguments = {}
        validators = []

        if metadata_field.get("optional"):
            field_arguments["required"] = False

        if any(
            length_limit in metadata_field
            for length_limit in ("min_length", "max_length", "length")
        ):
            validators.append(
                validate.Length(
                    min=metadata_field.get("min_length"),
                    max=metadata_field.get("max_length"),
                    equal=metadata_field.get("length"),
                )
            )

        dynamic_fields[metadata_field["name"]] = VALIDATORS[metadata_field["type"]](
            validate=validators, **field_arguments
        )

    questionnaire_metadata_schema = type(
        "QuestionnaireMetadataSchema", (Schema, StripWhitespaceMixin), dynamic_fields
    )(unknown=EXCLUDE)

    # The load method performs validation.
    return questionnaire_metadata_schema.load(claims["survey_metadata"]["data"])


def validate_runner_claims_v2(claims: Dict):
    """Validate claims required for runner to function"""
    runner_metadata_schema = RunnerMetadataSchema(unknown=EXCLUDE)
    return runner_metadata_schema.load(claims)