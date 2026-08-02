from __future__ import annotations

import unittest
from html.parser import HTMLParser

from yt_library import server


class TemplateDocument(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.ids: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.duplicate_ids: set[str] = set()
        self.headings: list[str] = []
        self._heading_tag = ""
        self._heading_text: list[str] = []
        self.feed(source)
        self.close()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids[element_id] = (tag, attributes)
        if tag in {"h1", "h2", "h3"}:
            self._heading_tag = tag
            self._heading_text = []

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._heading_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != self._heading_tag:
            return
        text = " ".join("".join(self._heading_text).split())
        if text:
            self.headings.append(text)
        self._heading_tag = ""
        self._heading_text = []

    def element(self, element_id: str) -> tuple[str, dict[str, str | None]]:
        return self.ids[element_id]

    def position(self, element_id: str) -> int:
        for index, (_tag, attributes) in enumerate(self.elements):
            if attributes.get("id") == element_id:
                return index
        raise KeyError(element_id)

    def matching(
        self,
        *,
        tag: str | None = None,
        class_name: str | None = None,
        attribute: str | None = None,
    ) -> list[tuple[str, dict[str, str | None]]]:
        matches = []
        for element_tag, attributes in self.elements:
            classes = (attributes.get("class") or "").split()
            if tag and element_tag != tag:
                continue
            if class_name and class_name not in classes:
                continue
            if attribute and attribute not in attributes:
                continue
            matches.append((element_tag, attributes))
        return matches


class TemplateDomTests(unittest.TestCase):
    def setUp(self) -> None:
        self.admin = TemplateDocument(server.ADMIN_HTML)
        self.index = TemplateDocument(server.INDEX_HTML)

    def test_templates_have_unique_ids_and_typed_controls(self) -> None:
        for document in (self.admin, self.index):
            self.assertEqual(document.duplicate_ids, set())
            for tag, attributes in document.elements:
                if tag in {"button", "input"}:
                    self.assertIn(
                        "type",
                        attributes,
                        f"{tag} #{attributes.get('id', '')} must declare its type",
                    )

    def test_admin_dom_exposes_settings_and_workstream_controls(self) -> None:
        expected_controls = {
            "serviceStatus": ("strong", None),
            "restartService": ("button", "button"),
            "advancedToggle": ("input", "checkbox"),
            "themeToggle": ("input", "checkbox"),
            "useProxy": ("input", "checkbox"),
            "proxyUrl": ("input", "text"),
            "initializeLibrary": ("button", "button"),
            "updateLibrary": ("button", "button"),
            "fetchVideoMetadata": ("button", "button"),
            "scanPlaylists": ("button", "button"),
            "fetchChannelMetadata": ("button", "button"),
            "startLiveHistory": ("button", "button"),
            "startWorkerQueue": ("button", "button"),
            "logPanel": ("div", None),
        }
        for element_id, (expected_tag, expected_type) in expected_controls.items():
            tag, attributes = self.admin.element(element_id)
            self.assertEqual(tag, expected_tag)
            if expected_type:
                self.assertEqual(attributes.get("type"), expected_type)

        self.assertEqual(
            self.admin.headings,
            [
                "YT Library Admin",
                "Update",
                "Cookies",
                "Videos",
                "Playlists",
                "Channels",
                "History",
                "Worker queue",
            ],
        )
        self.assertLess(
            self.admin.position("initializeLibrary"),
            self.admin.position("updateLibrary"),
        )
        self.assertLess(
            self.admin.position("updateLibrary"),
            self.admin.position("fetchVideoMetadata"),
        )

    def test_admin_advanced_and_cookie_dom_contract(self) -> None:
        advanced_workstreams = [
            element
            for element in self.admin.matching(tag="section", class_name="advanced-only")
            if "workstream" in (element[1].get("class") or "").split()
        ]
        self.assertEqual(len(advanced_workstreams), 5)

        tabs = {
            attributes["data-advanced-tab"]
            for _tag, attributes in self.admin.matching(attribute="data-advanced-tab")
        }
        panes = {
            attributes["data-advanced-pane"]
            for _tag, attributes in self.admin.matching(attribute="data-advanced-pane")
        }
        self.assertEqual(tabs, {"youtube", "google", "archivarix"})
        self.assertEqual(panes, tabs)
        self.assertNotIn("syncAccountDates", self.admin.ids)
        self.assertNotIn("historyFetchDaily", self.admin.ids)
        self.assertNotIn("backfillChannelFirstSeen", self.admin.ids)

    def test_browser_dom_preserves_primary_navigation_and_content_regions(self) -> None:
        expected_elements = {
            "history-nav": ("button", "button"),
            "search-nav": ("button", "button"),
            "search": ("input", "search"),
            "search-filters": ("div", None),
            "search-for-filters": ("div", None),
            "groups": ("nav", None),
            "view-title": ("h2", None),
            "search-progress-status": ("div", None),
            "view-meta": ("div", None),
            "refresh": ("button", "button"),
            "grid": ("section", None),
            "empty": ("div", None),
            "bottom-pager": ("div", None),
        }
        for element_id, (expected_tag, expected_type) in expected_elements.items():
            tag, attributes = self.index.element(element_id)
            self.assertEqual(tag, expected_tag)
            if expected_type:
                self.assertEqual(attributes.get("type"), expected_type)

        self.assertLess(
            self.index.position("history-nav"),
            self.index.position("search-nav"),
        )
        self.assertLess(
            self.index.position("view-meta"),
            self.index.position("refresh"),
        )
        script_sources = {
            attributes.get("src")
            for _tag, attributes in self.index.matching(tag="script")
        }
        self.assertIn("/theme.js", script_sources)


if __name__ == "__main__":
    unittest.main()
