from functools import cached_property, lru_cache
from typing import Any, Mapping, Optional, Type, Union

from flask import current_app
from flask import render_template as flask_render_template
from flask import request
from flask import session as cookie_session
from flask import url_for
from flask_babel import LazyString, get_locale, lazy_gettext
from flask_login import current_user

from app.globals import get_metadata, get_session_store
from app.helpers.language_helper import get_languages_context
from app.questionnaire import QuestionnaireSchema
from app.settings import ACCOUNT_SERVICE_BASE_URL
from app.survey_config import (
    BusinessSurveyConfig,
    CensusNISRASurveyConfig,
    CensusSurveyConfig,
    NorthernIrelandBusinessSurveyConfig,
    SocialSurveyConfig,
    SurveyConfig,
    WelshCensusSurveyConfig,
)
from app.survey_config.survey_type import SurveyType
from app.utilities.schema import load_schema_from_metadata


class ContextHelper:
    def __init__(
        self,
        language: str,
        is_post_submission: bool,
        include_csrf_token: bool,
        survey_config: SurveyConfig,
    ) -> None:
        self._language = language
        self._is_post_submission = is_post_submission
        self._include_csrf_token = include_csrf_token
        self._survey_config = survey_config
        self._survey_title = cookie_session.get(
            "survey_title", self._survey_config.survey_title
        )
        self._sign_out_url = url_for("session.get_sign_out")
        self._cdn_url = (
            f'{current_app.config["CDN_URL"]}{current_app.config["CDN_ASSETS_PATH"]}'
        )
        self._address_lookup_api = current_app.config["ADDRESS_LOOKUP_API_URL"]
        self._google_tag_manager_id = current_app.config.get("EQ_GOOGLE_TAG_MANAGER_ID")
        self._google_tag_manager_auth = current_app.config.get(
            "EQ_GOOGLE_TAG_MANAGER_AUTH"
        )
        self._survey_type = cookie_session.get("theme")

    @property
    def context(self) -> dict[str, Any]:
        context = {
            "sign_out_button_text": self._survey_config.sign_out_button_text,
            "account_service_my_account_url": self._survey_config.account_service_my_account_url,
            "account_service_log_out_url": self._survey_config.account_service_log_out_url,
            "account_service_todo_url": self._survey_config.account_service_todo_url,
            "contact_us_url": self._survey_config.contact_us_url,
            "thank_you_url": url_for("post_submission.get_thank_you"),
            "page_header": self.page_header_context,
            "service_links": self.service_links_context,
            "footer": self.footer_context,
            "languages": get_languages_context(self._language),
            "theme": self._survey_config.design_system_theme,
            "language_code": self._language,
            "survey_title": self._survey_title,
            "cdn_url": self._cdn_url,
            "address_lookup_api_url": self._address_lookup_api,
            "data_layer": self.data_layer_context,
            "include_csrf_token": self._include_csrf_token,
            "google_tag_manager_id": self._google_tag_manager_id,
            "google_tag_manager_auth": self._google_tag_manager_auth,
            "survey_type": self._survey_type,
        }
        if self._survey_type:
            context["cookie_settings_url"] = self._survey_config.cookie_settings_url
            context["cookie_domain"] = self._survey_config.cookie_domain

        return context

    @property
    def service_links_context(
        self,
    ) -> Optional[dict[str, Union[dict[str, str], list[dict]]]]:

        ru_ref = (
            metadata["ru_ref"] if (metadata := get_metadata(current_user)) else None
        )

        if service_links := self._survey_config.get_service_links(
            sign_out_url=self._sign_out_url,
            is_authenticated=current_user.is_authenticated,
            cookie_has_theme=bool(self._survey_type),
            ru_ref=ru_ref,
        ):
            return {
                "toggleServicesButton": {
                    "text": lazy_gettext("Menu"),
                    "ariaLabel": "Toggle services menu",
                },
                "itemsList": service_links,
            }

        return None

    @property
    def data_layer_context(
        self,
    ) -> list[dict]:
        tx_id = metadata["tx_id"] if (metadata := get_metadata(current_user)) else None

        return self._survey_config.get_data_layer(tx_id=tx_id)

    @property
    def page_header_context(self) -> dict[str, Union[bool, str, LazyString]]:
        context: dict[str, Union[bool, str, LazyString]] = {
            "orgLogo": f"{self._survey_config.page_header_logo}",
            "orgLogoAlt": f"{self._survey_config.page_header_logo_alt}",
            "title": self._survey_title
            if self._survey_title and self._survey_type
            else "ONS Surveys",
        }

        if self._survey_config.title_logo:
            context["titleLogo"] = self._survey_config.title_logo
        if self._survey_config.title_logo_alt:
            context["titleLogoAlt"] = self._survey_config.title_logo_alt
        if self._survey_config.custom_header_logo:
            context["customHeaderLogo"] = self._survey_config.custom_header_logo
        if self._survey_config.mobile_logo:
            context["orgMobileLogo"] = self._survey_config.mobile_logo

        return context

    @property
    def footer_context(self) -> dict[str, Any]:
        context = {
            "lang": self._language,
            "crest": self._survey_config.crest,
            "newTabWarning": lazy_gettext("The following links open in a new tab"),
            "copyrightDeclaration": {
                "copyright": self._survey_config.copyright_declaration,
                "text": self._survey_config.copyright_text,
            },
        }

        if self._footer_warning:
            context["footerWarning"] = self._footer_warning

        if footer_links := self._survey_config.get_footer_links(
            cookie_has_theme=bool(self._survey_type),
        ):
            context["rows"] = [{"itemsList": footer_links}]

        if footer_legal_links := self._survey_config.get_footer_legal_links(
            cookie_has_theme=bool(self._survey_type),
        ):
            context["legal"] = [{"itemsList": footer_legal_links}]

        if (
            self._survey_config.powered_by_logo
            or self._survey_config.powered_by_logo_alt
        ):
            context["poweredBy"] = {
                "logo": self._survey_config.powered_by_logo,
                "alt": self._survey_config.powered_by_logo_alt,
            }

        return context

    @cached_property
    def _footer_warning(self) -> Optional[str]:
        if self._is_post_submission:
            footer_warning: str = lazy_gettext(
                "Make sure you <a href='{sign_out_url}'>leave this page</a> or close your browser if using a shared device"
            ).format(sign_out_url=self._sign_out_url)

            return footer_warning


@lru_cache
def survey_config_mapping(
    *, theme: SurveyType, language: str, base_url: str, schema: QuestionnaireSchema
) -> SurveyConfig:
    survey_type_to_config: dict[SurveyType, Type[SurveyConfig]] = {
        SurveyType.DEFAULT: BusinessSurveyConfig,
        SurveyType.BUSINESS: BusinessSurveyConfig,
        SurveyType.HEALTH: SurveyConfig,
        SurveyType.SOCIAL: SocialSurveyConfig,
        SurveyType.NORTHERN_IRELAND: NorthernIrelandBusinessSurveyConfig,
        SurveyType.CENSUS: (
            WelshCensusSurveyConfig if language == "cy" else CensusSurveyConfig
        ),
        SurveyType.CENSUS_NISRA: CensusNISRASurveyConfig,
    }

    return survey_type_to_config[theme](
        base_url=base_url,
        schema=schema,
    )


def get_survey_config(
    *,
    base_url: Optional[str] = None,
    theme: Optional[SurveyType] = None,
    language: Optional[str] = None,
    schema: Optional[QuestionnaireSchema] = None,
) -> SurveyConfig:
    # The fallback to assigning SURVEY_TYPE to theme is only being added until
    # business feedback on the differentiation between theme and SURVEY_TYPE.

    if metadata := get_metadata(current_user):
        language = language or get_locale().language
        schema = load_schema_from_metadata(
            metadata_proxy=metadata, language_code=language
        )

    survey_theme = theme or get_survey_type()

    base_url = base_url or (
        cookie_session.get("account_service_base_url") or ACCOUNT_SERVICE_BASE_URL
    )

    return survey_config_mapping(
        theme=survey_theme,
        language=language,
        base_url=base_url,
        schema=schema,
    )


def render_template(template: str, **kwargs: Union[str, Mapping]) -> str:
    session_expires_at = None
    language = get_locale().language
    if session_store := get_session_store():
        if session_expiry := session_store.expiration_time:
            session_expires_at = session_expiry.isoformat()

    survey_config = get_survey_config()

    is_post_submission = request.blueprint == "post_submission"
    include_csrf_token = bool(
        request.url_rule
        and request.url_rule.methods
        and "POST" in request.url_rule.methods
    )

    context = ContextHelper(
        language, is_post_submission, include_csrf_token, survey_config
    ).context

    template = f"{template.lower()}.html"

    return flask_render_template(
        template,
        csp_nonce=request.csp_nonce,  # type: ignore
        session_expires_at=session_expires_at,
        **context,
        **kwargs,
    )


def get_survey_type() -> SurveyType:
    survey_type = cookie_session.get("theme", current_app.config["SURVEY_TYPE"])
    return SurveyType(survey_type)
