"""Tests for module 7 — email prose generation and rendering."""

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ccat.clarify.email_gen import (
    ANGEL_COLUMNS,
    ClarifyEmail,
    ClarifyError,
    FAMILY_COLUMNS,
    EmailDraft,
    build_prompt,
    default_subject,
    draft_path,
    generate_email,
    group_families,
    html_path,
    load_draft,
    parse_email_response,
    render_email,
    render_email_fragment,
    write_draft,
)
from ccat.clarify.server import generate_missing
from ccat.config import ClarifySpec, Config


def clarification_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build a clarification-table frame from partial rows.

    Args:
        rows: Row dicts with at least ``family_id``, ``angel_id``,
            ``first_name``, and ``require_clarification``.

    Returns:
        A frame with every column ``group_families`` consumes.
    """
    full: list[dict[str, Any]] = []
    for row in rows:
        full.append(
            {
                **{column: None for column in ANGEL_COLUMNS},
                **{column: None for column in FAMILY_COLUMNS},
                **row,
            }
        )
    return pl.DataFrame(full)


@pytest.fixture
def families() -> Any:
    """Two families (ids 1 and 2, one sharing two Angels)."""
    return group_families(
        clarification_frame(
            [
                {
                    "family_id": 2,
                    "angel_id": "2B",
                    "first_name": "Birch",
                    "require_clarification": "shoe size?",
                    "raw_dob": "2020-05-05",
                    "family_first_name": "Istiak",
                    "family_last_name": "Ahammed",
                    "family_phone": "555-0101",
                    "family_email": "istiak@example.com",
                },
                {
                    "family_id": 1,
                    "angel_id": "1A",
                    "first_name": "Aspen",
                    "require_clarification": "mattress size?",
                },
                {
                    "family_id": 1,
                    "angel_id": "1B",
                    "first_name": "Cedar",
                    "require_clarification": "pant size?",
                },
                {
                    "family_id": None,
                    "angel_id": "9A",
                    "first_name": "Orphan",
                    "require_clarification": "no family?",
                },
            ]
        )
    )


@pytest.fixture
def cfg(cfg: Config) -> Config:
    """Config with a small test system prompt."""
    return cfg.model_copy(
        update={
            "clarify": ClarifySpec(
                model="test/model",
                system_prompt="You write for the {{season}} Angel Tree.",
            )
        }
    )


def _response_json(email: ClarifyEmail) -> str:
    """Serialize a valid response exactly like the model returns it."""
    return json.dumps(
        {
            "greeting": email.greeting,
            "intro": email.intro,
            "angel_paragraphs": email.angel_paragraphs,
            "closing": email.closing,
        }
    )


class TestGroupFamilies:
    """``group_families`` grouping, ordering, and null handling."""

    def test_groups_and_orders(self, families: Any) -> None:
        """Families sort by id; Angels sort by angel_id within."""
        assert [f.family_id for f in families] == [1, 2]
        family_1 = families[0]
        assert [a.angel_id for a in family_1.angels] == ["1A", "1B"]
        assert [a.first_name for a in family_1.angels] == ["Aspen", "Cedar"]

    def test_missing_contact_fields_are_carried(self, families: Any) -> None:
        """A family whose contact join produced nulls still groups."""
        family_2 = families[1]
        assert family_2.family_first_name == "Istiak"
        assert family_2.family_phone == "555-0101"
        assert families[0].family_email is None

    def test_null_family_id_skipped(self, families: Any) -> None:
        """Rows without a family_id never reach a family."""
        assert all(
            angel.angel_id != "9A"
            for family in families
            for angel in family.angels
        )
        assert sum(len(f.angels) for f in families) == 3


class TestBuildPrompt:
    """``build_prompt`` content and contract."""

    def test_prompt_contains_everything(
        self, families: Any, cfg: Config
    ) -> None:
        """System prompt, season, contacts, and flags reach the model."""
        prompt = build_prompt(families[1], cfg)
        assert "You write for the 2026 Angel Tree." in prompt
        assert "istiak@example.com" in prompt
        assert "Birch" in prompt
        assert "shoe size?" in prompt
        record = json.loads(prompt.split("\n\n", 1)[1])
        assert record["family"]["family_id"] == 2
        assert record["angels"][0]["angel_id"] == "2B"
        # The per-angel payload never duplicates contact fields.
        assert "family_email" not in record["angels"][0]


class TestParseEmailResponse:
    """``parse_email_response`` validation."""

    def test_valid_response(self, families: Any) -> None:
        """A clean response validates."""
        family = families[0]
        parsed = parse_email_response(
            _response_json(
                ClarifyEmail(
                    greeting="Dear Caroline,",
                    intro="Hello!",
                    angel_paragraphs=["Q1", "Q2"],
                    closing="Thanks!",
                )
            ),
            family,
        )
        assert parsed.greeting == "Dear Caroline,"

    def test_wrong_paragraph_count_rejected(self, families: Any) -> None:
        """Paragraph count must match the family's Angels."""
        response = _response_json(
            ClarifyEmail(
                greeting="Dear Caroline,",
                intro="Hello!",
                angel_paragraphs=["Q1"],
                closing="Thanks!",
            )
        )
        with pytest.raises(ValueError, match="angel paragraph"):
            parse_email_response(response, families[0])

    def test_extra_key_rejected(self, families: Any) -> None:
        """Unknown keys are forbidden (extra='forbid')."""
        response = json.dumps(
            {
                "greeting": "Dear Caroline,",
                "intro": "Hello!",
                "angel_paragraphs": ["Q1", "Q2"],
                "closing": "Thanks!",
                "surprise": "extra",
            }
        )
        with pytest.raises(ValueError, match="surprise"):
            parse_email_response(response, families[0])

    def test_not_json_rejected(self, families: Any) -> None:
        """Non-JSON text raises ValueError."""
        with pytest.raises(ValueError, match="not a JSON object"):
            parse_email_response("not json at all", families[0])


class TestGenerateEmail:
    """``generate_email`` retry semantics (screening pattern)."""

    def test_retries_once_then_succeeds(
        self, families: Any, cfg: Config
    ) -> None:
        """One malformed response is retried; the second one wins."""
        family = families[0]
        good = _response_json(
            ClarifyEmail(
                greeting="Dear Caroline,",
                intro="Hello!",
                angel_paragraphs=["Q1", "Q2"],
                closing="Thanks!",
            )
        )
        calls: list[str] = []

        def send(prompt: str) -> str:
            """Yield garbage once, then a valid response."""
            calls.append(prompt)
            return "garbage {{" if len(calls) == 1 else good

        email = generate_email(family, cfg, send)
        assert email.intro == "Hello!"
        assert len(calls) == 2

    def test_two_failures_raise(self, families: Any, cfg: Config) -> None:
        """Two invalid attempts give up with ClarifyError."""

        def send(_prompt: str) -> str:
            """Return an always-malformed response."""
            return "nope"

        with pytest.raises(ClarifyError, match="family 1"):
            generate_email(families[0], cfg, send)


class TestRenderEmail:
    """``render_email`` and ``save_email`` output."""

    def test_render_contains_fragments(
        self, families: Any, cfg: Config
    ) -> None:
        """Subject, prose, signature, and recipient render."""
        family = families[1]
        email = ClarifyEmail(
            greeting="Dear Istiak,",
            intro="Hello and thank you!",
            angel_paragraphs=["About Birch: what shoe size?"],
            closing="Please reply.",
        )
        subject, html = render_email(family, email, cfg)
        assert "Copper Country Angel Tree 2026" in subject
        for fragment in (
            "Dear Istiak,",
            "Hello and thank you!",
            "About Birch: what shoe size?",
            "Please reply.",
            "The Copper Country Angel Tree Team",
            "istiak@example.com",
        ):
            assert fragment in html

    def test_missing_template_raises(self, families: Any, cfg: Config) -> None:
        """A missing template file fails with ClarifyError."""
        broken = cfg.model_copy(
            update={
                "clarify": cfg.clarify.model_copy(
                    update={"email_template_html_file": "data/missing.html"}
                )
            }
        )
        email = ClarifyEmail(
            greeting="Dear Istiak,",
            intro="Hello!",
            angel_paragraphs=["Q1"],
            closing="Thanks!",
        )
        with pytest.raises(ClarifyError, match="template"):
            render_email(families[1], email, broken)

    def test_render_uses_edited_subject(
        self, families: Any, cfg: Config
    ) -> None:
        """An explicit subject overrides the default template subject."""
        email = ClarifyEmail(
            greeting="Dear Istiak,",
            intro="Hello!",
            angel_paragraphs=["Q1"],
            closing="Thanks!",
        )
        subject, html = render_email(
            families[1], email, cfg, subject="Custom subject line"
        )
        assert subject == "Custom subject line"
        assert "Custom subject line" in html

    def test_render_fragment_is_solid_and_embeddable(
        self, families: Any, cfg: Config
    ) -> None:
        """Test the embeddable preview fragment.

        The fragment must have a solid background and no document
        wrapper, so the email always renders on white.
        """
        email = ClarifyEmail(
            greeting="Dear Istiak,",
            intro="Hello!",
            angel_paragraphs=["Q1"],
            closing="Thanks!",
        )
        fragment = render_email_fragment(families[1], email, cfg)
        assert f'id="email-body-{families[1].family_id}"' in fragment
        assert "background: #ffffff" in fragment
        assert "<body" not in fragment
        assert "<!DOCTYPE" not in fragment
        assert "Dear Istiak," in fragment

    def test_default_subject(self, cfg: Config) -> None:
        """The default subject carries the configured season."""
        assert "2026" in default_subject(cfg)

    def test_write_draft_saves_html_and_json(
        self, families: Any, cfg: Config, tmp_path: Path
    ) -> None:
        """write_draft renders the HTML and stores the draft JSON."""
        saved_cfg = cfg.model_copy(
            update={
                "clarify": cfg.clarify.model_copy(
                    update={"output_dir": str(tmp_path / "emails")}
                )
            }
        )
        family = families[1]
        draft = EmailDraft(
            subject="Custom subject",
            greeting="Dear Istiak,",
            intro="Hello!",
            angel_paragraphs=["Q1"],
            closing="Thanks!",
        )
        path = write_draft(saved_cfg, family, draft)
        assert path == tmp_path / "emails" / "family_2.html"
        assert path is not None and draft_path(saved_cfg, family).is_file()
        html = path.read_text()
        assert "Custom subject" in html
        assert "Dear Istiak," in html

    def test_draft_round_trip(
        self, families: Any, cfg: Config, tmp_path: Path
    ) -> None:
        """load_draft returns exactly what write_draft saved."""
        saved_cfg = cfg.model_copy(
            update={
                "clarify": cfg.clarify.model_copy(
                    update={"output_dir": str(tmp_path / "emails")}
                )
            }
        )
        family = families[0]
        assert load_draft(saved_cfg, family) is None
        draft = EmailDraft(
            subject="Subject",
            greeting="Dear friend,",
            intro="Intro",
            angel_paragraphs=["A", "B"],
            closing="Bye",
        )
        write_draft(saved_cfg, family, draft)
        assert load_draft(saved_cfg, family) == draft
        # HTML-only drafts (legacy) load as None.
        assert html_path(saved_cfg, family).is_file()
        draft_path(saved_cfg, family).unlink()
        assert load_draft(saved_cfg, family) is None

    def test_load_invalid_json_raises(
        self, families: Any, cfg: Config, tmp_path: Path
    ) -> None:
        """A corrupted draft JSON raises ClarifyError."""
        saved_cfg = cfg.model_copy(
            update={
                "clarify": cfg.clarify.model_copy(
                    update={"output_dir": str(tmp_path / "emails")}
                )
            }
        )
        family = families[0]
        out_dir = tmp_path / "emails"
        out_dir.mkdir(parents=True)
        draft_path(saved_cfg, family).write_text("{ not json")
        with pytest.raises(ClarifyError, match="invalid"):
            load_draft(saved_cfg, family)


class FakeSender:
    """Prompt sender returning valid prose for any family."""

    def __init__(self, junk: bool = False) -> None:
        """Configure whether every response is malformed."""
        self.junk = junk
        self.calls: list[str] = []

    def send(self, prompt: str) -> str:
        """Answer with one good paragraph per Angel in the prompt."""
        self.calls.append(prompt)
        if self.junk:
            return "not json"
        record = json.loads(prompt.split("\n\n", 1)[1])
        count = len(record["angels"])
        return json.dumps(
            {
                "greeting": "Dear friend,",
                "intro": "Hello!",
                "angel_paragraphs": [f"Paragraph {i}" for i in range(count)],
                "closing": "Thanks!",
            }
        )


class TestGenerateMissing:
    """Headless batch generation (``ccat clarify --generate``)."""

    def test_generates_missing_only(
        self, families: Any, cfg: Config, tmp_path: Path
    ) -> None:
        """Families without a draft are filled in; existing skipped."""
        cfg = _redirect(cfg, tmp_path)
        # Family 1 already has a saved draft html on disk.
        existing = html_path(cfg, families[0])
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("<p>existing</p>")
        sender = FakeSender()
        done, failed = generate_missing(cfg, families, sender)
        assert done == [families[1].family_id]
        assert failed == []
        assert existing.read_text() == "<p>existing</p>"
        assert html_path(cfg, families[1]).is_file()
        assert draft_path(cfg, families[1]).is_file()
        assert len(sender.calls) == 1

    def test_overwrite_regenerates_all(
        self, families: Any, cfg: Config, tmp_path: Path
    ) -> None:
        """With overwrite every family gets a fresh draft."""
        cfg = _redirect(cfg, tmp_path)
        existing = html_path(cfg, families[0])
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("<p>existing</p>")
        done, failed = generate_missing(
            cfg, families, FakeSender(), overwrite=True
        )
        assert done == [1, 2]
        assert failed == []
        assert "Paragraph 0" in html_path(cfg, families[0]).read_text()

    def test_failures_are_collected(
        self, families: Any, cfg: Config, tmp_path: Path
    ) -> None:
        """A family whose generation fails is reported, not raised."""
        cfg = _redirect(cfg, tmp_path)
        done, failed = generate_missing(cfg, families, FakeSender(junk=True))
        assert done == []
        assert failed == [1, 2]
        assert not html_path(cfg, families[0]).is_file()


def _redirect(cfg: Config, tmp_path: Path) -> Config:
    """Point ``clarify.output_dir`` at the test temp dir."""
    return cfg.model_copy(
        update={
            "clarify": cfg.clarify.model_copy(
                update={"output_dir": str(tmp_path / "emails")}
            )
        }
    )
