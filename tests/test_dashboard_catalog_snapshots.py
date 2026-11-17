"""Observed listing identities reach the real static catalog without granting access."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_dashboard_ui import run_ui_script

from quota_broker.dashboard import PROVIDER_INVENTORY, Dashboard

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = {"nvidia": 33, "openrouter": 724, "huggingface": 132, "sambanova": 5, "cerebras": 2}
ACCOUNT = {"google": 64, "groq": 11, "mistral": 53, "cloudflare": 75, "openai": 96}


def asset():
    # Exercise the production size/schema gate; demo suppression remains separate.
    result = Dashboard._provider_inventory(SimpleNamespace(source=SimpleNamespace(kind="upstream")))
    assert result is not None
    assert PROVIDER_INVENTORY.stat().st_size <= 512 * 1024
    return result


def listing(provider):
    dated = "2026-11-16" if provider in PUBLIC else "2026-11-17"
    return json.loads((ROOT / "docs/provider-inventory" / dated / (provider + ".json")).read_text())


@pytest.mark.parametrize("provider,count", {**PUBLIC, **ACCOUNT}.items())
def test_latest_official_listing_preserves_all_observed_identities_and_unknowns(provider, count):
    snapshot = listing(provider)
    basis = "public_catalog" if provider in PUBLIC else "key_catalog"
    profile = next(p for p in asset()["providers"] if p["id"] == provider)
    catalogs = [
        c
        for c in profile["catalogs"]
        if c["source"] == snapshot["source"]
        and c["basis"] == basis
        and c["checked_at"] == snapshot["checked_at"]
    ]
    assert len(catalogs) == 1
    catalog = catalogs[0]
    assert {m["id"] for m in catalog["models"]} == {m["model"] for m in snapshot["models"]}
    assert len(catalog["models"]) == count
    by_id = {m["id"]: m for m in catalog["models"]}
    for row in snapshot["models"]:
        variants = by_id[row["model"]]["variants"]
        assert row["capability"] in {v["task"] for v in variants}
        if row["capability"] == "unknown" and not any(
            f.endswith(("_input", "_output")) for f in row["features"]
        ):
            assert {"task": "unknown", "inputs": [], "outputs": []} in variants
        assert not any(
            k in by_id[row["model"]]
            for k in ("price", "free_eligible", "registered", "execution_allowed", "target_id")
        )
    if provider in ("cloudflare", "huggingface"):
        assert "部分" in catalog["coverage"]
    if provider == "cloudflare":
        assert all(text in catalog["coverage"] for text in ("75", "334", "第 2 頁為空"))


def test_api_descriptions_and_distinct_docs_retirement_weight_scopes_are_preserved():
    kept = [
        {
            "id": p["id"],
            "name": p["name"],
            "apis": p["apis"],
            "catalogs": [c for c in p["catalogs"] if c.get("basis") == "docs"],
        }
        for p in asset()["providers"]
    ]
    digest = hashlib.sha256(
        json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == "c8ab223b51e09ae6342667af9f6221bb58c6afd5d543317574f7f16b8b576d5e"


def test_real_catalog_search_reads_updated_asset_without_execution_or_extra_requests():
    run_ui_script(r"""
(async()=>{
 await tick();
 const path=require('node:path');
 snapshot.provider_inventory=JSON.parse(fs.readFileSync(path.join(path.dirname(process.argv[1]),'provider-inventory.json'),'utf8'));
 context.render(snapshot);get('model-view-catalog').handlers.click();
 for(const [provider,model] of [['nvidia','z-ai/glm-5.3-flash'],['cloudflare','@cf/google/gemma-4-26b-a4b-it'],['openai','gpt-5.1'],['huggingface','deepseek-ai/DeepSeek-V4-Pro']]){
  get('catalog-provider').value=provider;get('catalog-search').value=model;
  get('catalog-provider').handlers.change();
  assert.ok(get('catalog-models').textContent.includes(model),provider+' identity must be searchable');
  assert.match(get('catalog-models').textContent,/執行權限未驗證|帳戶存取未核對/);
 }
 get('catalog-provider').value='cloudflare';get('catalog-provider').handlers.change();
 assert.match(get('catalog-sources').textContent,/75.*334.*第 2 頁為空/);
 assert.equal(calls.length,1,'static catalog must not dispatch or fetch new metadata');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
