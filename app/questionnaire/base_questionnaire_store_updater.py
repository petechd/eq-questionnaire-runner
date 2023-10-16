from collections import defaultdict
from itertools import combinations
from typing import Iterable, Mapping

from ordered_set import OrderedSet
from werkzeug.datastructures import ImmutableDict

from app.data_models import AnswerValueTypes, CompletionStatus, QuestionnaireStore
from app.data_models.answer_store import Answer
from app.data_models.relationship_store import RelationshipDict, RelationshipStore
from app.questionnaire import QuestionnaireSchema
from app.questionnaire.location import Location, SectionKey
from app.questionnaire.questionnaire_schema import AnswerDependent
from app.questionnaire.router import Router
from app.utilities.types import DependentSection


class BaseQuestionnaireStoreUpdater:
    """
    Base component responsible for any actions that need to happen as a result of updating the questionnaire_store.
    This should only be used over QuestionnaireStoreUpdater if there is no knowledge of the current location
    """

    EMPTY_ANSWER_VALUES: tuple[None, list, str, dict] = (None, [], "", {})

    def __init__(
        self,
        schema: QuestionnaireSchema,
        questionnaire_store: QuestionnaireStore,
        router: Router,
    ):
        self._schema = schema
        self._questionnaire_store = questionnaire_store
        self._answer_store = self._questionnaire_store.answer_store
        self._list_store = self._questionnaire_store.list_store
        self._progress_store = self._questionnaire_store.progress_store
        self._router = router

        self.dependent_block_id_by_section_key: Mapping[
            SectionKey, set[str]
        ] = defaultdict(set)
        self.dependent_sections: set[DependentSection] = set()

    def save(self) -> None:
        if self.is_dirty():
            self._questionnaire_store.save()

    def is_dirty(self) -> bool:
        return bool(
            (
                self._answer_store.is_dirty
                or self._list_store.is_dirty
                or self._progress_store.is_dirty
            )
        )

    def update_relationships_answer(
        self,
        relationship_store: RelationshipStore,
        relationships_answer_id: str,
    ) -> None:
        self._answer_store.add_or_update(
            # Type ignore: serialize returns a list of typed dicts, so it is a valid answer type
            Answer(relationships_answer_id, relationship_store.serialize())  # type: ignore
        )

    def remove_completed_relationship_locations_for_list_name(
        self, list_name: str
    ) -> None:
        if target_relationship_collectors := self._get_relationship_collectors_by_list_name(
            list_name
        ):
            for target in target_relationship_collectors:
                block_id = target["id"]
                section_id = self._schema.get_section_for_block_id(block_id)["id"]  # type: ignore
                self._progress_store.remove_completed_location(
                    Location(section_id, block_id)
                )

    def _update_relationship_question_completeness(self, list_name: str) -> None:
        relationship_collectors = self._get_relationship_collectors_by_list_name(
            list_name
        )

        if not relationship_collectors:
            return None

        list_items = self._list_store[list_name]

        for collector in relationship_collectors:
            relationship_answer_id = self._schema.get_first_answer_id_for_block(
                collector["id"]
            )
            if relationship_answers := self._get_relationships_in_answer_store(
                relationship_answer_id
            ):
                pairs = {
                    (answer["list_item_id"], answer["to_list_item_id"])
                    for answer in relationship_answers
                }

                expected_pairs = set(combinations(list_items, 2))
                if expected_pairs == pairs:
                    section_id = self._schema.get_section_for_block_id(collector["id"])[
                        "id"
                    ]  # type: ignore
                    location = Location(section_id, collector["id"])
                    self._progress_store.add_completed_location(location)

    def _get_relationship_collectors_by_list_name(
        self, list_name: str
    ) -> list[ImmutableDict] | None:
        return self._schema.get_relationship_collectors_by_list_name(list_name)

    def _get_relationships_in_answer_store(
        self, relationship_answer_id: str
    ) -> list[RelationshipDict]:
        return self._answer_store.get_answer(relationship_answer_id).value  # type: ignore

    def remove_answers(
        self, answer_ids: list[str], list_item_id: str | None = None
    ) -> None:
        for answer_id in answer_ids:
            self._answer_store.remove_answer(answer_id, list_item_id=list_item_id)

    def add_list_item(self, list_name: str) -> str:
        new_list_item_id = self._list_store.add_list_item(list_name)
        self.remove_completed_relationship_locations_for_list_name(list_name)
        return new_list_item_id

    def remove_list_item_data(self, list_name: str, list_item_id: str) -> None:
        """Remove answers from the answer store, remove list item progress from the progress store and update the list store to remove it.
        Any related relationship answers are re-evaluated for completeness.
        """
        self._list_store.delete_list_item(list_name, list_item_id)

        self._answer_store.remove_all_answers_for_list_item_ids(list_item_id)

        if answers := self._get_relationship_answers_for_list_name(list_name):
            self._remove_relationship_answers_for_list_item_id(list_item_id, answers)
            self._update_relationship_question_completeness(list_name)

        self._progress_store.remove_progress_for_list_item_id(list_item_id=list_item_id)
        self.capture_dependencies_for_list_item_removal(list_name)
        self.capture_dependent_sections_for_list(list_name)

    def capture_dependencies_for_list_item_removal(self, list_name: str) -> None:
        """
        If an item is removed from a list, dependencies of any child blocks need to be captured and their progress updated.
        (E.g. dynamic-answers depending on a child block could have validation, so need revisiting if an item is removed)
        """
        for list_collector in self._schema.get_list_collectors_for_list(
            for_list=list_name
        ):
            if list_name in self._schema.supplementary_lists:
                self._capture_dependencies_for_supplementary_list(list_name)
            child_blocks = (
                list_collector.get("add_block"),
                list_collector.get("remove_block"),
                *list_collector.get("repeating_blocks", []),
            )
            for child_block in child_blocks:
                if child_block:
                    for answer_id in self._schema.get_answer_ids_for_block(
                        child_block["id"]
                    ):
                        self._capture_block_dependencies_for_answer(answer_id)

    def _get_relationship_answers_for_list_name(
        self, list_name: str
    ) -> list[Answer] | None:
        associated_relationship_collectors = (
            self._get_relationship_collectors_by_list_name(list_name)
        )
        if not associated_relationship_collectors:
            return None

        relationship_answer_ids = [
            self._schema.get_first_answer_id_for_block(block["id"])
            for block in associated_relationship_collectors
        ]

        return self._answer_store.get_answers_by_answer_id(relationship_answer_ids)

    def update_same_name_items(
        self, list_name: str, same_name_answer_ids: list[str] | None
    ) -> None:
        if not same_name_answer_ids:
            return

        same_name_items = set()
        people_names: dict[str, str] = {}

        list_model = self._questionnaire_store.list_store[list_name]

        for current_list_item_id in list_model:
            answers = self._questionnaire_store.answer_store.get_answers_by_answer_id(
                answer_ids=same_name_answer_ids, list_item_id=current_list_item_id
            )
            current_names = [answer.value.casefold() for answer in answers if answer]  # type: ignore
            current_list_item_name = " ".join(current_names)

            if matching_list_item_id := people_names.get(current_list_item_name):
                same_name_items |= {current_list_item_id, matching_list_item_id}
            else:
                people_names[current_list_item_name] = current_list_item_id

        list_model.same_name_items = list(same_name_items)  # type: ignore

    def _remove_relationship_answers_for_list_item_id(
        self, list_item_id: str, answers: list
    ) -> None:
        for answer in answers:
            answers_to_keep = [
                value
                for value in answer.value
                if list_item_id not in {value["to_list_item_id"], value["list_item_id"]}
            ]
            answer.value = answers_to_keep
            self._answer_store.add_or_update(answer)

    def update_section_status(
        self, *, is_complete: bool, section_key: SectionKey
    ) -> bool:
        status = (
            CompletionStatus.COMPLETED if is_complete else CompletionStatus.IN_PROGRESS
        )
        return self._progress_store.update_section_status(status, section_key)

    def _update_answer(
        self,
        answer_id: str,
        list_item_id: str | None,
        answer_value: AnswerValueTypes,
    ) -> bool:
        answer_value_to_store = (
            {
                key: value
                for key, value in answer_value.items()
                if value not in self.EMPTY_ANSWER_VALUES
            }
            if isinstance(answer_value, dict)
            else answer_value
        )

        if answer_value_to_store in self.EMPTY_ANSWER_VALUES:
            return self._answer_store.remove_answer(
                answer_id, list_item_id=list_item_id
            )

        return self._answer_store.add_or_update(
            Answer(
                answer_id=answer_id,
                list_item_id=list_item_id,
                value=answer_value_to_store,
            )
        )

    def _capture_dependencies_for_supplementary_list(self, list_name: str) -> None:
        """
        In the case of a supplementary list being used for a block with dynamic answers,
        there is no list collector with add/remove blocks that will act as a dependency for the dynamic answers
        So the blocks are tracked separately and this handles adding them as dependencies for progress.

        If the dependent block is in a repeating section, this is deleted entirely, so not relevant to progress
        """
        for block_id in self._schema.get_block_dependencies_for_supplementary_list(
            list_name
        ):
            section_id: str = self._schema.get_section_id_for_block_id(block_id)  # type: ignore
            self.dependent_block_id_by_section_key[SectionKey(section_id)].add(block_id)
            for answer_id in self._schema.get_answer_ids_for_block(block_id):
                self._capture_block_dependencies_for_answer(answer_id)

    def _capture_block_dependencies_for_answer(self, answer_id: str) -> None:
        """Captures a unique list of block ids that are dependents of the provided answer id.

        The block_ids are mapped to the section key. Dependencies in a repeating section use the list items
        for the repeating list when creating the section key.

        Blocks are captured regardless of whether they are complete. This avoids fetching the completed blocks
        multiples times, as you may have multiple dependencies for one block which may also apply to each item in the list.
        However, when updating the progress store, the block ids are checked to ensure they exist in the progress store.
        """
        dependencies: set[AnswerDependent] = self._schema.answer_dependencies.get(
            answer_id, set()
        )

        is_repeating_answer = self._schema.is_answer_in_repeating_section(answer_id)

        for dependency in dependencies:
            for list_item_id in self._get_list_item_ids_for_dependency(
                dependency, is_repeating_answer
            ):
                if dependency.answer_id:
                    self._answer_store.remove_answer(
                        dependency.answer_id, list_item_id=list_item_id
                    )

                self.dependent_block_id_by_section_key[
                    SectionKey(dependency.section_id, list_item_id)
                ].add(dependency.block_id)

    def _get_list_item_ids_for_dependency(
        self, dependency: AnswerDependent, is_repeating_answer: bool | None = False
    ) -> list[str] | list[None]:
        """
        For a repeating answer, and a dependency within that repeat,
        the only affected list item id is the current one. Since the base updater doesn't have the current location
        this is not allowed.

        If the dependency is in a repeat, and the data is non-repeating, all list items are affected.
        """
        if dependency.for_list:
            if is_repeating_answer:
                raise ValueError(
                    "Not valid to use BaseQuestionnaireStoreUpdater to capture a repeating answer dependency"
                )
            return self._list_store[dependency.for_list].items
        return [None]

    def _capture_section_dependencies_for_answer(self, answer_id: str) -> None:
        """Captures a unique list of section ids that are dependents of the provided answer id."""

        answer_id_section_dependents = (
            self._schema.when_rules_section_dependencies_by_answer
        )

        for section_id in answer_id_section_dependents.get(answer_id, {}):
            if repeating_list := self._schema.get_repeating_list_for_section(
                section_id
            ):
                for list_item_id in self._list_store[repeating_list].items:
                    self.dependent_sections.add(
                        DependentSection(section_id, list_item_id)
                    )
            else:
                self.dependent_sections.add(DependentSection(section_id))

    def _update_section_dependencies(self, dependent_sections: Iterable) -> None:
        for section_id in dependent_sections:
            if repeating_list := self._schema.get_repeating_list_for_section(
                section_id
            ):
                for list_item_id in self._list_store[repeating_list].items:
                    self.dependent_sections.add(
                        DependentSection(section_id, list_item_id)
                    )
            else:
                self.dependent_sections.add(DependentSection(section_id))

    def update_progress_for_dependent_sections(self) -> None:
        """Removes dependent blocks from the progress store and updates the progress to IN_PROGRESS.
        Section progress is not updated for the current location as it is handled by `handle_post` on block handlers.
        """
        evaluated_dependents: list[tuple] = []

        chronological_dependents = self._get_chronological_section_dependents()

        for section in chronological_dependents:
            if (
                section.section_id,
                section.list_item_id,
            ) not in self.started_section_keys():
                continue

            if (
                section.section_id,
                section.list_item_id,
            ) not in evaluated_dependents:
                self._evaluate_dependents(
                    dependent_section=section, evaluated_dependents=evaluated_dependents
                )
                evaluated_dependents.append((section.section_id, section.list_item_id))

    def _evaluate_dependents(
        self,
        *,
        dependent_section: DependentSection,
        evaluated_dependents: list[tuple],
    ) -> None:
        is_path_complete = dependent_section.is_complete
        if is_path_complete is None:
            is_path_complete = self._router.is_path_complete(
                self._router.routing_path(dependent_section.section_key)
            )

        if self.update_section_status(
            is_complete=is_path_complete, section_key=dependent_section.section_key
        ):
            dependents_of_dependent: OrderedSet = self._schema.when_rules_section_dependencies_by_section_for_progress_value_source.get(
                dependent_section.section_id, OrderedSet()
            )
            for dependent_section_id in dependents_of_dependent:
                if repeating_list := self._schema.get_repeating_list_for_section(
                    dependent_section_id
                ):
                    for item_id in self._list_store[repeating_list].items:
                        if (
                            dependent_section_id,
                            item_id,
                        ) not in evaluated_dependents:
                            self._evaluate_dependent_of_dependents(
                                dependent_section_id=dependent_section_id,
                                list_item_id=item_id,
                                evaluated_dependents=evaluated_dependents,
                            )
                elif (
                    dependent_section_id,
                    dependent_section.list_item_id,
                ) not in evaluated_dependents:
                    self._evaluate_dependent_of_dependents(
                        dependent_section_id=dependent_section_id,
                        evaluated_dependents=evaluated_dependents,
                    )

    def _evaluate_dependent_of_dependents(
        self,
        dependent_section_id: str,
        evaluated_dependents: list[tuple],
        list_item_id: str | None = None,
    ) -> None:
        self._evaluate_dependents(
            dependent_section=DependentSection(
                section_id=dependent_section_id,
                list_item_id=list_item_id,
            ),
            evaluated_dependents=evaluated_dependents,
        )
        evaluated_dependents.append((dependent_section_id, list_item_id))

    def _is_current_location(self, section_id: str, list_item_id: str | None) -> bool:
        """Overridden by QuestionnaireStoreUpdater to return True
        if the provided section_id and list_item_id match the current location"""
        return False

    def _get_section_ids_dependent_on_list(self, list_name: str) -> list[str]:
        """Overridden by QuestionnaireStoreUpdater to include the current section too"""
        return self._schema.get_section_ids_dependent_on_list(list_name)

    def remove_dependent_blocks_and_capture_dependent_sections(self) -> None:
        """Removes dependent blocks from the progress store."""

        for (
            section_key,
            blocks_to_remove,
        ) in self.dependent_block_id_by_section_key.items():
            if section_key not in self.started_section_keys():
                continue

            section_id, list_item_id = section_key

            blocks_removed = False
            for block_id in blocks_to_remove:
                location = Location(
                    section_id=section_id,
                    list_item_id=list_item_id,
                    block_id=block_id,
                )
                blocks_removed |= self._progress_store.remove_completed_location(
                    location
                )

            if blocks_removed and not self._is_current_location(
                section_id, list_item_id
            ):
                # Since this section key will be marked as incomplete, any `DependentSection` with is_complete as `None`
                # can be removed as we do not need to re-evaluate progress as we already know the section would be incomplete.
                dependent = DependentSection(section_id, list_item_id)
                if dependent in self.dependent_sections:
                    self.dependent_sections.remove(dependent)

                self.dependent_sections.add(
                    DependentSection(section_id, list_item_id, is_complete=False)
                )

    def capture_dependent_sections_for_list(self, list_name: str) -> None:
        """
        Capture the sections which depend on the given list.
        This should include repeating sections and the section in which the list is collected.
        It should not include list repeating blocks.
        """
        section_ids = self._get_section_ids_dependent_on_list(list_name)

        for (
            section_id,
            list_item_id,
        ) in self.started_section_keys(section_ids=section_ids):
            # if the section is not repeating but there is a list_item_id, then it must be a repeating block
            if not self._schema.get_repeat_for_section(section_id) and list_item_id:
                continue
            self.dependent_sections.add(
                DependentSection(
                    section_id,
                    list_item_id,
                )
            )

    def started_section_keys(
        self, section_ids: Iterable[str] | None = None
    ) -> list[SectionKey]:
        return self._progress_store.started_section_keys(section_ids)

    def _get_chronological_section_dependents(self) -> list:
        sections = list(self._schema.get_section_ids())
        return sorted(
            self.dependent_sections, key=lambda x: sections.index(x.section_id)
        )