from __future__ import annotations

import re
from dataclasses import asdict, dataclass

ACTIVE_PATTERNS = [
    re.compile(r"prochaine tranche active(?:\s+du\s+flux\s+principal)? est\s+\*{0,2}`?#?(\d+[A-Z]?)", re.IGNORECASE),
    re.compile(r"prochaine tranche(?: produit)?\s+(?:est|:)\s+\*{0,2}`?#?(\d+[A-Z]?)", re.IGNORECASE),
    re.compile(r"#?(\d+[A-Z]?)\s+est\s+maintenant\s+la\s+tranche\s+active", re.IGNORECASE),
    re.compile(r"#?(\d+[A-Z]?)\s+est\s+(?:maintenant\s+)?la\s+tranche\s+produit\s+active", re.IGNORECASE),
    re.compile(r"tranche\s+(?:produit\s+)?active\s*[:=]\s*\*{0,2}`?#?(\d+[A-Z]?)", re.IGNORECASE),
]

DONE_WORDS = re.compile(r"\b(?:done|termin[ée]e?s?|compl[ée]t[ée]e?s?|livr[ée]e?s?)\b", re.IGNORECASE)
GATE_DONE_WORDS = re.compile(
    r"\b(?:satisfait(?:e|es|s)?|termin(?:é|ée|és|ées)|complét(?:é|ée|és|ées)|effectu(?:é|ée|és|ées)|valid(?:é|ée|és|ées))\b",
    re.IGNORECASE,
)
PIPELINE_ARCHITECTURE = "ARCHITECTURE_GATE"
PIPELINE_ENVIRONMENT = "ENVIRONMENT_GATE"
PIPELINE_WORK = "WORK"
PIPELINE_MAIN = "MAIN"
PIPELINE_PARALLEL = "PARALLEL"
PIPELINE_STATUS_DONE = "DONE"
PIPELINE_STATUS_READY = "READY"
PIPELINE_STATUS_BLOCKED = "BLOCKED"
COCKPIT_PIPELINE_START = "<!-- COCKPIT_PIPELINE_V1 -->"
COCKPIT_PIPELINE_END = "<!-- /COCKPIT_PIPELINE_V1 -->"
COCKPIT_PIPELINE_HEADER = ("KEY", "TYPE", "STATUS", "PARENT", "LANE", "TITLE")
VALID_PIPELINE_KINDS = {
    PIPELINE_WORK,
    PIPELINE_ARCHITECTURE,
    PIPELINE_ENVIRONMENT,
}
VALID_PIPELINE_STATUSES = {
    PIPELINE_STATUS_DONE,
    PIPELINE_STATUS_READY,
    PIPELINE_STATUS_BLOCKED,
}
VALID_PIPELINE_LANES = {PIPELINE_MAIN, PIPELINE_PARALLEL}
ARCHITECTURE_GATE_WORDS = re.compile(
    r"\b(?:ASTRA|analyse\s+architectur(?:e|ale)|revue\s+architecturale|architecture\s+gate)\b",
    re.IGNORECASE,
)
ENVIRONMENT_GATE_WORDS = re.compile(
    r"\b(?:environnement(?:al|ale|aux)?|infra(?:structure)?|validation\s+(?:VM|environnementale|infrastructure)|VM\s+Ubuntu|déploiement\s+réel)\b",
    re.IGNORECASE,
)


@dataclass
class WorkItem:
    key: str
    title: str
    done: bool
    marker: str | None = None
    issue_number: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PipelineStep:
    key: str
    title: str
    kind: str
    done: bool
    lane: str = PIPELINE_MAIN
    marker: str | None = None
    issue_number: int | None = None
    status: str | None = None
    rationale: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PipelineContract:
    present: bool
    valid: bool
    source: str
    steps: list[PipelineStep]
    errors: list[str]

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "valid": self.valid,
            "source": self.source,
            "errors": list(self.errors),
        }


def normalize_key(value: str) -> str:
    value = value.strip().upper()
    return value[1:] if value.startswith("#") else value


def numeric_issue(key: str) -> int | None:
    match = re.match(r"^(\d+)", normalize_key(key))
    return int(match.group(1)) if match else None


def extract_declared_active(body: str) -> str | None:
    for pattern in ACTIVE_PATTERNS:
        match = pattern.search(body)
        if match:
            return normalize_key(match.group(1))
    return None


def _clean_markdown(line: str) -> str:
    return line.replace("**", "").replace("`", "").strip()


def _active_order_section(body: str) -> str:
    marker = re.search(r"Ordre actif\s*: ?", body, re.IGNORECASE)
    if not marker:
        marker = re.search(r"ordre recommandé devient\s*: ?", body, re.IGNORECASE)
    if not marker:
        return body
    section = body[marker.start():]
    stop = re.search(r"\n###\s+", section)
    return section[: stop.start()] if stop else section


def _canonical_pipeline_key(
    key: str,
    kind: str,
    parent_issue: int,
) -> str | None:
    if kind == PIPELINE_WORK:
        if not re.fullmatch(r"\d+[A-Z]?", key):
            return None
        return key if numeric_issue(key) == parent_issue else None
    if kind == PIPELINE_ARCHITECTURE:
        match = re.fullmatch(r"ASTRA-(\d+)", key)
        return key if match and int(match.group(1)) == parent_issue else None
    if kind == PIPELINE_ENVIRONMENT:
        match = re.fullmatch(r"ENV-(\d+)", key)
        return key if match and int(match.group(1)) == parent_issue else None
    return None


def parse_cockpit_pipeline(body: str) -> PipelineContract:
    start_count = body.count(COCKPIT_PIPELINE_START)
    end_count = body.count(COCKPIT_PIPELINE_END)
    present = bool(start_count or end_count)
    if not present:
        return PipelineContract(
            present=False,
            valid=True,
            source="legacy",
            steps=[],
            errors=[],
        )

    errors: list[str] = []
    if start_count != 1 or end_count != 1:
        errors.append(
            "COCKPIT_PIPELINE_V1 doit contenir exactement un marqueur de début et un marqueur de fin."
        )
        return PipelineContract(
            present=True,
            valid=False,
            source="canonical_v1",
            steps=[],
            errors=errors,
        )

    start = body.index(COCKPIT_PIPELINE_START) + len(COCKPIT_PIPELINE_START)
    end = body.index(COCKPIT_PIPELINE_END)
    if end <= start:
        errors.append("Le marqueur de fin COCKPIT_PIPELINE_V1 précède le début.")
        return PipelineContract(
            present=True,
            valid=False,
            source="canonical_v1",
            steps=[],
            errors=errors,
        )

    raw_lines = [
        line.strip()
        for line in body[start:end].splitlines()
        if line.strip()
    ]
    if not raw_lines:
        errors.append("COCKPIT_PIPELINE_V1 ne contient aucune ligne.")
        return PipelineContract(True, False, "canonical_v1", [], errors)

    header = tuple(cell.strip().upper() for cell in raw_lines[0].split("|"))
    if header != COCKPIT_PIPELINE_HEADER:
        errors.append(
            "En-tête COCKPIT_PIPELINE_V1 invalide; attendu: "
            + " | ".join(COCKPIT_PIPELINE_HEADER)
            + "."
        )
        return PipelineContract(True, False, "canonical_v1", [], errors)

    steps: list[PipelineStep] = []
    seen_keys: set[str] = set()
    for line_number, raw in enumerate(raw_lines[1:], start=2):
        cells = [cell.strip() for cell in raw.split("|")]
        if len(cells) != 6:
            errors.append(
                f"Ligne {line_number}: 6 colonnes requises (KEY | TYPE | STATUS | PARENT | LANE | TITLE)."
            )
            continue
        key_raw, kind_raw, status_raw, parent_raw, lane_raw, title = cells
        key = normalize_key(key_raw)
        kind = kind_raw.upper()
        status = status_raw.upper()
        lane = lane_raw.upper()

        if not key:
            errors.append(f"Ligne {line_number}: KEY vide.")
            continue
        if key.startswith("PR") or key.startswith("CI"):
            errors.append(
                f"Ligne {line_number}: KEY '{key_raw}' invalide; PR/CI ne sont pas des identités d'étape."
            )
            continue
        if key in seen_keys:
            errors.append(f"Ligne {line_number}: KEY dupliquée '{key}'.")
            continue
        if kind not in VALID_PIPELINE_KINDS:
            errors.append(f"Ligne {line_number}: TYPE invalide '{kind_raw}'.")
            continue
        if status not in VALID_PIPELINE_STATUSES:
            errors.append(f"Ligne {line_number}: STATUS invalide '{status_raw}'.")
            continue
        if lane not in VALID_PIPELINE_LANES:
            errors.append(f"Ligne {line_number}: LANE invalide '{lane_raw}'.")
            continue
        parent_match = re.fullmatch(r"#(\d+)", parent_raw)
        if not parent_match:
            errors.append(
                f"Ligne {line_number}: PARENT invalide '{parent_raw}'; attendu #<issue>."
            )
            continue
        parent_issue = int(parent_match.group(1))
        canonical_key = _canonical_pipeline_key(key, kind, parent_issue)
        if canonical_key is None:
            errors.append(
                f"Ligne {line_number}: KEY '{key_raw}' incohérente avec TYPE '{kind}' et PARENT '{parent_raw}'."
            )
            continue
        if not title:
            errors.append(f"Ligne {line_number}: TITLE vide.")
            continue

        seen_keys.add(canonical_key)
        steps.append(
            PipelineStep(
                key=canonical_key,
                title=title,
                kind=kind,
                done=status == PIPELINE_STATUS_DONE,
                lane=lane,
                marker="✅" if status == PIPELINE_STATUS_DONE else None,
                issue_number=parent_issue,
                status=status,
                rationale=None,
            )
        )

    main_steps = [step for step in steps if step.lane == PIPELINE_MAIN]
    if not main_steps:
        errors.append("COCKPIT_PIPELINE_V1 doit contenir au moins une étape MAIN.")
    else:
        ready_indexes = [
            index
            for index, step in enumerate(main_steps)
            if step.status == PIPELINE_STATUS_READY
        ]
        non_done_indexes = [
            index
            for index, step in enumerate(main_steps)
            if step.status != PIPELINE_STATUS_DONE
        ]
        if non_done_indexes:
            if len(ready_indexes) != 1:
                errors.append(
                    "La lane MAIN doit avoir exactement une étape READY tant qu'elle contient du travail non DONE."
                )
            else:
                ready_index = ready_indexes[0]
                if ready_index != non_done_indexes[0]:
                    errors.append(
                        "L'étape READY de la lane MAIN doit être la première étape non DONE."
                    )
                if any(
                    step.status != PIPELINE_STATUS_DONE
                    for step in main_steps[:ready_index]
                ):
                    errors.append(
                        "Toutes les étapes MAIN précédant READY doivent être DONE."
                    )
                if any(
                    step.status != PIPELINE_STATUS_BLOCKED
                    for step in main_steps[ready_index + 1 :]
                ):
                    errors.append(
                        "Toutes les étapes MAIN suivant READY doivent être BLOCKED."
                    )
        elif ready_indexes:
            errors.append(
                "Une lane MAIN entièrement DONE ne doit contenir aucune étape READY."
            )

    return PipelineContract(
        present=True,
        valid=not errors,
        source="canonical_v1",
        steps=steps if not errors else [],
        errors=errors,
    )


def resolve_canonical_pipeline_source(body: str) -> PipelineContract:
    """Resolve a dedicated machine source without any legacy Markdown fallback."""
    marker_inventory = re.findall(
        r"<!--\s*/?COCKPIT_PIPELINE_V(\d+)\s*-->",
        body,
    )
    unsupported_versions = sorted(
        {value for value in marker_inventory if value != "1"},
        key=int,
    )
    if unsupported_versions:
        version_text = ", ".join(f"V{value}" for value in unsupported_versions)
        return PipelineContract(
            present=bool(marker_inventory),
            valid=False,
            source="canonical_v1",
            steps=[],
            errors=[
                "Source canonique invalide: version COCKPIT_PIPELINE non supportée "
                f"({version_text}); une source dédiée ne peut contenir que COCKPIT_PIPELINE_V1."
            ],
        )

    canonical = parse_cockpit_pipeline(body)
    if canonical.present:
        return canonical

    return PipelineContract(
        present=False,
        valid=False,
        source="canonical_v1",
        steps=[],
        errors=[
            "Source canonique invalide: le bloc COCKPIT_PIPELINE_V1 est absent. "
            "Aucun fallback Markdown legacy n'est permis."
        ],
    )


def resolve_product_pipeline(body: str) -> PipelineContract:
    canonical = parse_cockpit_pipeline(body)
    if canonical.present:
        return canonical
    return PipelineContract(
        present=False,
        valid=True,
        source="legacy",
        steps=product_pipeline(body),
        errors=[],
    )


def render_cockpit_pipeline(steps: list[PipelineStep]) -> str:
    lines = [
        COCKPIT_PIPELINE_START,
        " | ".join(COCKPIT_PIPELINE_HEADER),
    ]
    for step in steps:
        if step.issue_number is None:
            raise ValueError(f"L'étape {step.key} n'a pas de parent GitHub.")
        status = step.status or (
            PIPELINE_STATUS_DONE if step.done else PIPELINE_STATUS_BLOCKED
        )
        lines.append(
            " | ".join(
                (
                    step.key,
                    step.kind,
                    status,
                    f"#{step.issue_number}",
                    step.lane,
                    step.title,
                )
            )
        )
    lines.append(COCKPIT_PIPELINE_END)
    return "\n".join(lines)


def _heading_level(line: str) -> int | None:
    match = re.match(r"^(?P<marks>#{1,6})\s+", line.strip())
    return len(match.group("marks")) if match else None


def _product_pipeline_section(body: str) -> str:
    lines = body.splitlines()
    start = None
    level = None
    for index, raw in enumerate(lines):
        cleaned = _clean_markdown(raw)
        heading_level = _heading_level(cleaned)
        if (
            heading_level is not None
            and re.search(r"\b(?:suite|pipeline)\s+produit\b", cleaned, re.IGNORECASE)
        ):
            start = index
            level = heading_level
            break
    if start is None or level is None:
        return ""

    end = len(lines)
    for index in range(start + 1, len(lines)):
        next_level = _heading_level(_clean_markdown(lines[index]))
        if next_level is not None and next_level <= level:
            end = index
            break
    return "\n".join(lines[start:end])


def _pipeline_code_block(section: str) -> str:
    for match in re.finditer(
        r"```(?:text)?\s*\n(?P<body>.*?)\n```",
        section,
        re.IGNORECASE | re.DOTALL,
    ):
        block = match.group("body")
        if re.search(r"#\d+[A-Z]?\b", block):
            return block
    return ""


def _pipeline_kind(text: str) -> str:
    if ARCHITECTURE_GATE_WORDS.search(text):
        return PIPELINE_ARCHITECTURE
    if ENVIRONMENT_GATE_WORDS.search(text):
        return PIPELINE_ENVIRONMENT
    return PIPELINE_WORK


def _pipeline_done(text: str, kind: str) -> bool:
    if "✅" in text:
        return True
    if kind == PIPELINE_WORK:
        return bool(DONE_WORDS.search(text))
    return bool(GATE_DONE_WORDS.search(text))


def _pipeline_table_done(label: str, status: str, kind: str) -> bool:
    if kind != PIPELINE_WORK:
        return _pipeline_done(f"{label} | {status}", kind)

    cleaned_label = _clean_markdown(label)
    cleaned_status = _clean_markdown(status)
    if "✅" in cleaned_label or "✅" in cleaned_status:
        return True
    if DONE_WORDS.search(cleaned_label):
        return True

    # For WORK rows, completion words in dependency prose must not complete
    # the row itself. A NEXT row may mention a completed dependency without
    # being complete itself.
    return bool(
        re.match(
            r"^\s*(?:[🟢🟠🟡🔵🟦]\s*)?"
            r"(?:done|termin[ée]e?s?|compl[ée]t[ée]e?s?|livr[ée]e?s?)\b",
            cleaned_status,
            re.IGNORECASE,
        )
    )


def _pipeline_identity(kind: str, issue_number: int, key: str) -> str:
    if kind == PIPELINE_ARCHITECTURE:
        return f"ASTRA-{issue_number}"
    return key


def _parse_pipeline_fragment(
    fragment: str,
    *,
    lane: str = PIPELINE_MAIN,
) -> PipelineStep | None:
    cleaned = _clean_markdown(fragment).strip(" -")
    match = re.search(r"#(?P<key>\d+[A-Z]?)\b", cleaned, re.IGNORECASE)
    if not match:
        return None
    key = normalize_key(match.group("key"))
    issue_number = numeric_issue(key)
    if issue_number is None:
        return None
    kind = _pipeline_kind(cleaned)
    marker = "✅" if "✅" in cleaned else None
    return PipelineStep(
        key=_pipeline_identity(kind, issue_number, key),
        title=cleaned,
        kind=kind,
        done=_pipeline_done(cleaned, kind),
        lane=lane,
        marker=marker,
        issue_number=issue_number,
        status=None,
        rationale=None,
    )


def _pipeline_table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if cells[0].lower() in {"étape", "etape"}:
            continue
        rows.append(cells)
    return rows


def _matching_pipeline_step(
    steps: list[PipelineStep],
    label: str,
) -> PipelineStep | None:
    parsed = _parse_pipeline_fragment(label)
    if not parsed:
        return None
    for step in steps:
        if (
            step.issue_number == parsed.issue_number
            and step.kind == parsed.kind
            and (
                step.kind != PIPELINE_WORK
                or step.key == parsed.key
                or (step.key.isdigit() and parsed.key.isdigit())
            )
        ):
            return step
    return None


def _issue_references(line: str) -> list[re.Match[str]]:
    return list(
        re.finditer(
            r"(?<!PR )(?<!CI )#(?P<key>\d+[A-Z]?)\b",
            line,
            re.IGNORECASE,
        )
    )


def _explicit_work_done(body: str, key: str) -> bool:
    normalized = normalize_key(key)
    issue = numeric_issue(normalized)
    if issue is None:
        return False
    if re.search(r"[A-Z]$", normalized):
        return any(
            item.key == normalized and item.done
            for item in subitems_from_text(body, issue)
        )

    for raw in body.splitlines():
        line = _clean_markdown(raw)
        references = _issue_references(line)
        if not references or normalize_key(references[0].group("key")) != normalized:
            continue

        # A numeric WORK is complete only when the completion marker belongs to
        # that item's own segment. Completion prose about an earlier item must
        # never mark later issue references complete.
        first = references[0]
        next_start = references[1].start() if len(references) > 1 else len(line)
        scoped = line[first.start():next_start]
        prefix = line[:first.start()]

        # A gate may target the same issue number as the following WORK.
        # Its completion must never mark that WORK complete.
        if _pipeline_kind(line) != PIPELINE_WORK:
            continue
        if "✅" in prefix or "✅" in scoped or DONE_WORDS.search(scoped):
            return True
    return False


def _explicit_gate_done(body: str, step: PipelineStep) -> bool:
    if step.issue_number is None:
        return step.done
    if step.kind == PIPELINE_ARCHITECTURE:
        pattern = (
            rf"[^\n]*(?:ASTRA|architectur)[^\n]*#{step.issue_number}\b"
            rf"[^\n]*(?:satisfait(?:e)?|termin(?:é|ée)|complét(?:é|ée)|effectu(?:é|ée)|valid(?:é|ée))"
        )
        reverse = (
            rf"[^\n]*(?:satisfait(?:e)?|termin(?:é|ée)|complét(?:é|ée)|effectu(?:é|ée)|valid(?:é|ée))"
            rf"[^\n]*(?:ASTRA|architectur)[^\n]*#{step.issue_number}\b"
        )
        return bool(
            re.search(pattern, body, re.IGNORECASE)
            or re.search(reverse, body, re.IGNORECASE)
        )
    if step.kind == PIPELINE_ENVIRONMENT:
        pattern = (
            rf"[^\n]*#{step.issue_number}\b[^\n]*"
            rf"(?:VM\s+Ubuntu|environnement|infra(?:structure)?|déploiement)"
            rf"[^\n]*(?:termin(?:é|ée)|complét(?:é|ée)|effectu(?:é|ée)|valid(?:é|ée))"
        )
        return bool(re.search(pattern, body, re.IGNORECASE))
    return False


def product_pipeline(body: str) -> list[PipelineStep]:
    section = _product_pipeline_section(body)
    if not section:
        return []
    block = _pipeline_code_block(section)
    if not block:
        return []

    steps: list[PipelineStep] = []
    seen: set[tuple[str, int | None]] = set()
    for raw in block.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"(?:↓|→|->|\s)+", line):
            continue
        line_lane = (
            PIPELINE_PARALLEL
            if re.match(
                r"^(?:En\s+parall[eè]le\b|Livr[ée]\s+en\s+parall[eè]le\b)",
                _clean_markdown(line),
                re.IGNORECASE,
            )
            else PIPELINE_MAIN
        )
        fragments = re.split(r"\s*(?:→|->)\s*", line)
        for fragment in fragments:
            step = _parse_pipeline_fragment(fragment, lane=line_lane)
            if not step:
                continue
            identity = (step.key, step.issue_number)
            if identity in seen:
                continue
            seen.add(identity)
            steps.append(step)

    for cells in _pipeline_table_rows(section):
        label = cells[0]
        status = cells[1] if len(cells) > 1 else ""
        rationale = cells[2] if len(cells) > 2 else ""
        step = _matching_pipeline_step(steps, label)
        if not step:
            continue
        combined = " | ".join((label, status, rationale))
        step.status = _clean_markdown(status) or None
        step.rationale = _clean_markdown(rationale) or None
        explicit_kind = _pipeline_kind(combined)
        if explicit_kind != PIPELINE_WORK or step.kind == PIPELINE_WORK:
            if step.kind != explicit_kind:
                step.kind = explicit_kind
                step.key = _pipeline_identity(
                    explicit_kind,
                    int(step.issue_number or 0),
                    normalize_key(re.search(r"#(\d+[A-Z]?)", label, re.IGNORECASE).group(1)),
                )
        if len(label.strip()) > len(step.title):
            step.title = _clean_markdown(label)
        if _pipeline_table_done(label, status, step.kind):
            step.done = True
            step.marker = "✅"

    for step in steps:
        if step.done:
            continue
        if step.kind == PIPELINE_WORK and _explicit_work_done(body, step.key):
            step.done = True
            step.marker = "✅"
        elif step.kind != PIPELINE_WORK and _explicit_gate_done(body, step):
            step.done = True
            step.marker = "✅"
    return steps


def merge_pipeline_work_status(
    pipeline: list[PipelineStep],
    work_items: list[WorkItem],
) -> list[PipelineStep]:
    by_key = {item.key: item for item in work_items}
    merged = [PipelineStep(**step.to_dict()) for step in pipeline]
    for step in merged:
        if step.kind != PIPELINE_WORK:
            continue
        work = by_key.get(step.key)
        if not work:
            continue
        if work.done:
            step.done = True
            step.marker = "✅"
        elif step.marker is None and work.marker is not None:
            step.marker = work.marker
    return merged


def pipeline_window(
    pipeline: list[PipelineStep],
    *,
    next_count: int = 3,
) -> dict:
    main_steps = [step for step in pipeline if step.lane != PIPELINE_PARALLEL]
    parallel = [
        step.to_dict()
        for step in pipeline
        if step.lane == PIPELINE_PARALLEL and not step.done
    ]
    pending_index = next(
        (index for index, step in enumerate(main_steps) if not step.done),
        None,
    )
    completed_count = sum(1 for step in pipeline if step.done)
    if pending_index is None:
        return {
            "completed_count": completed_count,
            "now": None,
            "parallel": parallel,
            "next": [],
            "later": [],
        }
    return {
        "completed_count": completed_count,
        "now": main_steps[pending_index].to_dict(),
        "parallel": parallel,
        "next": [
            step.to_dict()
            for step in main_steps[pending_index + 1 : pending_index + 1 + next_count]
        ],
        "later": [
            step.to_dict()
            for step in main_steps[pending_index + 1 + next_count :]
        ],
    }


def top_level_items(body: str) -> list[WorkItem]:
    section = _active_order_section(body)
    lines = section.splitlines()
    pattern = re.compile(r"^\s*\d+\.\s*(?P<marker>✅|🟡|⏳|⚠️|⬜)?\s*#(?P<key>\d+)\s*[—–-]\s*(?P<title>.+?)\s*$")
    matches: list[tuple[int, re.Match[str]]] = []
    seen: set[str] = set()

    for index, raw in enumerate(lines):
        line = _clean_markdown(raw)
        match = pattern.match(line)
        if not match or match.group("key") in seen:
            continue
        seen.add(match.group("key"))
        matches.append((index, match))

    items: list[WorkItem] = []
    for position, (line_index, match) in enumerate(matches):
        next_index = matches[position + 1][0] if position + 1 < len(matches) else len(lines)
        block = "\n".join(lines[line_index:next_index])
        key = match.group("key")
        marker = match.group("marker") or None
        completed_in_text = bool(
            re.search(r"\bBloc terminé\b", block, re.IGNORECASE)
            or re.search(rf"#{re.escape(key)}\s+est\s+[^\n]*(?:termin|complét)", block, re.IGNORECASE)
        )
        items.append(
            WorkItem(
                key=key,
                title=match.group("title").strip(),
                done=marker == "✅" or completed_in_text,
                marker="✅" if completed_in_text and marker is None else marker,
                issue_number=int(key),
            )
        )
    return items


def subitems_from_text(body: str, parent: int | str) -> list[WorkItem]:
    parent_text = str(parent)
    patterns = [
        re.compile(
            rf"^\s*#{{2,6}}\s*(?P<marker>✅|🟡|⏳|⚠️|⬜)?\s*#?(?P<key>{re.escape(parent_text)}[A-Z])\s*[—–-]\s*(?P<title>.+?)\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^\s*-\s*(?P<marker>✅|🟡|⏳|⚠️|⬜)?\s*#?(?P<key>{re.escape(parent_text)}[A-Z])\s*[—–-]\s*(?P<title>.+?)\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^\s*-\s*\[(?P<checked>[xX ])\]\s*#?(?P<key>{re.escape(parent_text)}[A-Z])\s*[—–-]\s*(?P<title>.+?)\s*$",
            re.IGNORECASE,
        ),
    ]
    items: dict[str, WorkItem] = {}
    order: list[str] = []

    for raw in body.splitlines():
        line = _clean_markdown(raw)
        for pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            key = normalize_key(match.group("key"))
            marker = match.groupdict().get("marker") or None
            checked = match.groupdict().get("checked")
            done = marker == "✅" or checked in {"x", "X"}
            title = match.group("title").strip()
            if re.search(r"\bDONE\b", title, re.IGNORECASE):
                done = True
            elif DONE_WORDS.search(title) and re.search(
                r"\b(?:PR\s*#?\d+|CI\s*#?\d+|termin|complét|livr)",
                title,
                re.IGNORECASE,
            ):
                done = True
            if key not in items:
                items[key] = WorkItem(key=key, title=title, done=done, marker="✅" if done else marker, issue_number=int(parent_text))
                order.append(key)
            else:
                current = items[key]
                current.done = current.done or done
                if done:
                    current.marker = "✅"
                if len(title) > len(current.title):
                    current.title = title
            break
    return [items[key] for key in order]


def merge_subitems(primary: list[WorkItem], secondary: list[WorkItem]) -> list[WorkItem]:
    merged: dict[str, WorkItem] = {item.key: WorkItem(**item.to_dict()) for item in primary}
    order = [item.key for item in primary]
    for item in secondary:
        if item.key not in merged:
            merged[item.key] = WorkItem(**item.to_dict())
            order.append(item.key)
            continue
        current = merged[item.key]
        current.done = current.done or item.done
        if item.done:
            current.marker = "✅"
        elif current.marker is None and item.marker is not None:
            current.marker = item.marker
        if not current.title and item.title:
            current.title = item.title
    return [merged[key] for key in order]


def first_unfinished(items: list[WorkItem]) -> WorkItem | None:
    return next((item for item in items if not item.done), None)


def focus_items(top: list[WorkItem], subitems: list[WorkItem], parent: int) -> list[WorkItem]:
    parent_index = next((index for index, item in enumerate(top) if item.key == str(parent)), None)
    following = top[parent_index + 1 : parent_index + 4] if parent_index is not None else []
    return subitems + following if subitems else top


def active_block(body: str, parent: int | str) -> str:
    parent_text = str(parent)
    section = _active_order_section(body)
    lines = section.splitlines()
    start = None
    for idx, raw in enumerate(lines):
        line = _clean_markdown(raw)
        if re.match(rf"^\s*\d+\.\s*(?:✅|🟡|⏳|⚠️|⬜)?\s*#{re.escape(parent_text)}\b", line):
            start = idx
            break
    if start is None:
        return ""

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = _clean_markdown(lines[idx])
        if re.match(r"^\s*\d+\.\s*(?:✅|🟡|⏳|⚠️|⬜)?\s*#\d+\b", line):
            end = idx
            break
    return "\n".join(lines[start:end])


def referenced_issue_numbers(body: str, parent: int | str, limit: int = 12) -> list[int]:
    block = active_block(body, parent)
    if not block:
        return [int(parent)] if str(parent).isdigit() else []
    values: list[int] = []
    for match in re.finditer(r"#(\d+)\b", block):
        number = int(match.group(1))
        if number not in values:
            values.append(number)
        if len(values) >= limit:
            break
    return values


def has_explicit_block_order(issue_body: str, roadmap_block: str, subitems: list[WorkItem]) -> bool:
    if len(subitems) < 2:
        return False
    combined = f"{issue_body}\n{roadmap_block}"
    return bool(
        re.search(r"Ordre\s+obligatoire\s*:", combined, re.IGNORECASE)
        or re.search(r"Ordre\s*:\s*\*{0,2}\d+[A-Z]\s*(?:→|->)", combined, re.IGNORECASE)
    )


def agents_allow_chaining(agents_text: str) -> bool:
    lowered = agents_text.lower()
    return "chained execution" in lowered and "same approved work block" in lowered


def referenced_adrs(text: str, available_names: list[str]) -> list[str]:
    references = {match.upper() for match in re.findall(r"ADR-\d{3}", text, re.IGNORECASE)}
    return [name for name in available_names if any(name.upper().startswith(ref) for ref in references)]
