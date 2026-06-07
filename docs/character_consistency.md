# Character Consistency

A *character* in Manga Autopilot is a Pydantic model that records hair
colour, eye colour, outfit must-keep / must-avoid, an optional LoRA, an
optional IP-Adapter reference, and a palette. The image-generation
planner locks these tokens into the prompt so that the same character
looks consistent across pages.

## Levels

| Level | Method                          | Status |
|------:|---------------------------------|--------|
| 1     | Prompt-level `mustKeep`         | Always |
| 2     | Character sheet generation      | Always |
| 3     | Reference image upload          | Always |
| 4     | IP-Adapter binding              | Always |
| 5     | LoRA strength per character     | Always |
| 6     | Face detailer post-process      | Hookable |
| 7     | Face-embedding similarity       | Future  |

## Prompt locking

`build_character_prompt(character)` puts the `mustKeep` tokens first
followed by appearance, outfit, and the consistency prompt. The
`mustAvoid` tokens are joined into the negative prompt.

```python
from manga_autopilot.services.character_service import build_character_prompt

prompt = build_character_prompt(character)
# "silver long hair, blue eyes, silver long hair, blue eyes, black armor, ..."
```

## Reference images

Upload via the HTTP API:

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/projects/demo/characters/alice/references \
  -H 'Content-Type: application/json' \
  -d '{
        "filename": "ref.png",
        "label": "front",
        "data_base64": "..."
      }'
```

The image is stored at
`{storage_root}/projects/demo/assets/characters/alice/ref_NNN.png` and
added to `character.reference_images`.

## IP-Adapter binding

Add an `ip_adapter_ref` to the character:

```python
from manga_autopilot.models.character import AssetRef

character.ip_adapter_ref = AssetRef(
    asset_id="ip",
    path="assets/characters/alice/ref_front.png",
)
```

`build_ip_adapter_overrides(character)` returns a dict
`{"ip_adapter_image": "...", "ip_adapter_strength": 0.8}` that the
workflow runner applies automatically.

## LoRA binding

```python
from manga_autopilot.models.character import LoraRef

character.lora = LoraRef(name="alice_lora", strength_model=0.7, strength_clip=0.5)
```

`build_lora_overrides(character)` returns the `lora_name` /
`lora_strength_*` overrides.

## Character sheet

`sheet_prompt_for_view(character, "front")` produces the LLM-style
prompt that the sheet-generation workflow consumes. The expected file
naming is `reference_{view}.png` (front, side, back, face, expression,
outfit) per spec section 13.5.
