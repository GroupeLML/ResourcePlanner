from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERY = ROOT / "app" / "infrastructure" / "sql" / "asset_query.py"
MODELS = ROOT / "app" / "application" / "query_models.py"
PANEL = ROOT / "frontend" / "src" / "AssetPlanningPanel.tsx"
API = ROOT / "frontend" / "src" / "api.ts"
ASSET_API = ROOT / "frontend" / "src" / "assetApi.ts"
DIRECT = ROOT / "frontend" / "src" / "DirectAssetReservationForm.tsx"


def test_858_candidate_authority_stays_backend_and_privacy_safe() -> None:
    query = QUERY.read_text(encoding="utf-8")
    models = MODELS.read_text(encoding="utf-8")

    assert "overlapping_asset_occupations" in query
    assert "AssetOccupation(" in query
    assert "eligible_asset_ids" in models
    assert "conflicting_allocation_ids" not in models
    assert "continuous_beneficiary_id" not in models
    assert "request_candidate_asset_ids" in query


def test_858_frontend_consumes_eligibility_without_reconstructing_continuous_state() -> None:
    panel = PANEL.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    asset_api = ASSET_API.read_text(encoding="utf-8")

    assert "eligible_asset_ids: string[]" in api
    assert "getAssetRequirementReservationCandidates" in asset_api
    assert "eligibleAssetIdSet.has(asset.id)" in panel
    assert "Aucune unité disponible pour cette période." in panel
    assert "L’unité actuellement affichée n’est pas disponible pour cette nouvelle période." in panel
    assert "resource_period_mode === \"CONTINUOUS\"" not in panel.split("const compatible =", 1)[1].split("const [operatorSelected", 1)[0]


def test_858_does_not_widen_direct_reservation_form() -> None:
    direct = DIRECT.read_text(encoding="utf-8")

    assert "eligible_asset_ids" not in direct
    assert "getAssetRequirementReservationCandidates" not in direct
