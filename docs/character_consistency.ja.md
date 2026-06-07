# キャラクター一貫性

> 🌐 **言語 / Language:** [English](character_consistency.md) | **日本語** (このファイル)

Manga Autopilot における「キャラクター」は、髪型 / 瞳の色 /
服装の must-keep / must-avoid、任意の LoRA、任意の IP-Adapter 参照、
カラーパレットなどを保持する Pydantic モデルです。画像生成プランナー
がこれらのトークンをプロンプトに固定することで、同じキャラクターを
ページ間で一貫した見た目に保ちます。

## レベル

| Level | 手法                             | 状況 |
|------:|----------------------------------|------|
| 1     | プロンプトレベルの `mustKeep`     | 常時 |
| 2     | キャラクターシート生成            | 常時 |
| 3     | 参照画像のアップロード            | 常時 |
| 4     | IP-Adapter バインディング         | 常時 |
| 5     | キャラクター単位の LoRA 強度      | 常時 |
| 6     | 顔 Detailer 後処理                | 連携可 |
| 7     | 顔埋め込み類似度                  | 将来 |

## プロンプト固定

`build_character_prompt(character)` は `mustKeep` トークンを先頭に置き、
その後ろに容姿・服装・一貫性プロンプトを続けます。`mustAvoid` トークンは
ネガティブプロンプトに連結されます。

```python
from manga_autopilot.services.character_service import build_character_prompt

prompt = build_character_prompt(character)
# "silver long hair, blue eyes, silver long hair, blue eyes, black armor, ..."
```

## 参照画像

HTTP API でアップロードします。

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/projects/demo/characters/alice/references \
  -H 'Content-Type: application/json' \
  -d '{
        "filename": "ref.png",
        "label": "front",
        "data_base64": "..."
      }'
```

画像は
`{storage_root}/projects/demo/assets/characters/alice/ref_NNN.png` に
保存され、`character.reference_images` に追記されます。

## IP-Adapter バインディング

キャラクターに `ip_adapter_ref` を追加します。

```python
from manga_autopilot.models.character import AssetRef

character.ip_adapter_ref = AssetRef(
    asset_id="ip",
    path="assets/characters/alice/ref_front.png",
)
```

`build_ip_adapter_overrides(character)` は
`{"ip_adapter_image": "...", "ip_adapter_strength": 0.8}` という
辞書を返し、ワークフローランナーが自動で適用します。

## LoRA バインディング

```python
from manga_autopilot.models.character import LoraRef

character.lora = LoraRef(name="alice_lora", strength_model=0.7, strength_clip=0.5)
```

`build_lora_overrides(character)` は `lora_name` /
`lora_strength_*` のオーバーライドを返します。

## キャラクターシート

`sheet_prompt_for_view(character, "front")` は、シート生成ワークフローが
受け取る LLM 風プロンプトを生成します。期待されるファイル命名は
`reference_{view}.png` (front, side, back, face, expression, outfit) で、
spec セクション 13.5 に準拠します。
