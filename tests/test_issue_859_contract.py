from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx"
STYLES = ROOT / "frontend" / "src" / "styles.css"
DIRECT = ROOT / "frontend" / "src" / "DirectAssetReservationForm.tsx"


def test_859_master_detail_is_local_ui_state_over_full_snapshot_population() -> None:
    panel = PANEL.read_text(encoding="utf-8")

    assert 'useState<string | null>(null)' in panel
    assert "snapshot.asset_requirements.filter" in panel
    assert "visibleApprovedRequirements.find" in panel
    assert "setSelectedRequirementId(null)" in panel
    assert 'data-testid="approved-asset-master-list"' in panel
    assert 'data-testid="approved-asset-detail"' in panel
    assert "Aucun besoin d'actif sélectionné" in panel
    assert panel.count("<AssetRequirementDetail") == 1
    assert 'data-testid="unreserved-asset-requirements-toggle"' not in panel
    assert 'data-testid="reserved-asset-requirements-toggle"' not in panel


def test_859_search_uses_only_existing_snapshot_fields_and_is_accent_tolerant() -> None:
    panel = PANEL.read_text(encoding="utf-8")

    assert '.normalize("NFD")' in panel
    assert "snapshot.demands.forEach" in panel
    assert "demand.project_name" in panel
    for field in (
        "requirement.project_number",
        "requirement.demand_number",
        "requirement.asset_type_code",
        "requirement.asset_type_label",
    ):
        assert field in panel
    assert "fetch(" not in panel.split("const searchedRequirements", 1)[1].split(
        "const searchedUnreservedCount", 1
    )[0]


def test_859_global_unassigned_metric_uses_unfiltered_snapshot_population() -> None:
    panel = PANEL.read_text(encoding="utf-8")

    assert "const unassigned = snapshot.asset_requirements.filter(" in panel
    assert "(requirement) => !requirement.asset_id" in panel
    metric_block = panel.split("const unassigned =", 1)[1].split("return (", 1)[0]
    assert "searchedRequirements" not in metric_block
    assert "visibleApprovedRequirements" not in metric_block
    assert "approvedStatusFilter" not in metric_block
    assert "selectedRequirementId" not in metric_block


def test_859_status_is_still_only_asset_id_and_counts_precede_status_filter() -> None:
    panel = PANEL.read_text(encoding="utf-8")

    assert "searchedRequirements.filter((row) => !row.asset_id).length" in panel
    assert "searchedRequirements.length - searchedUnreservedCount" in panel
    assert 'approvedStatusFilter === "unreserved"' in panel
    assert 'approvedStatusFilter === "reserved"' in panel
    assert "? !row.asset_id" in panel
    assert "? Boolean(row.asset_id)" in panel
    assert 'aria-pressed={approvedStatusFilter === "unreserved"}' in panel
    assert 'aria-pressed={approvedStatusFilter === "reserved"}' in panel


def test_859_accessibility_and_responsive_contract() -> None:
    panel = PANEL.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'role="listbox"' in panel
    assert 'role="option"' in panel
    assert "aria-selected={selected}" in panel
    assert 'aria-label="Détail du besoin d’actif sélectionné"' in panel
    assert 'aria-label="Filtrer les besoins approuvés"' in panel
    assert "Rechercher les besoins approuvés" not in panel
    assert ".asset-approved-master-item:focus-visible" in styles
    assert "@media (max-width: 720px)" in styles
    assert ".asset-approved-master-detail { grid-template-columns: 1fr; }" in styles


def test_859_preserves_858_detail_and_direct_reservation_boundaries() -> None:
    panel = PANEL.read_text(encoding="utf-8")
    direct = DIRECT.read_text(encoding="utf-8")

    assert "requirement.eligible_asset_ids" in panel
    assert "getAssetRequirementReservationCandidates" in panel
    assert "Aucune unité disponible pour cette période." in panel
    assert "asset.id === requirement.asset_id" in panel
    assert "DirectAssetReservationForm" in panel
    assert "approvedSearch" not in direct
    assert "approvedStatusFilter" not in direct
