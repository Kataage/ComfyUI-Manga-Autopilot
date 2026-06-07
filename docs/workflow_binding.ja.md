# ワークフローバインディング

> 🌐 **言語 / Language:** [English](workflow_binding.md) | **日本語** (このファイル)

Manga Autopilot における「ワークフロー」とは、ComfyUI の API 形式の
JSON ファイルです。**ワークフローレジストリ** はこれらをディスクに永続化し、
ライブの ComfyUI `object_info` に対して検証し、型付けされた入力を
バインドして、Autopilot やコマ単位のランナーがオーバーライド付きで
呼び出せるようにします。

## ファイルレイアウト

各ワークフローは単一ファイルとして保存されます。

```text
{storage_root}/workflows/{workflow_id}.json
```

サイドカーのインデックスファイル `workflows.json` に登録済み ID の
リストが保持されます。

## API 形式ワークフローのみ

レジストリは UI 保存形式を受け取らないため、ComfyUI 側で
**「Save (API Format)」** でエクスポートした JSON を使用してください。

## ワークフローの登録

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/workflows \
  -H 'Content-Type: application/json' \
  -d @workflows/anime_t2i_api.json
```

レスポンスには採番された `workflow_id` が含まれます。検証は
登録時に自動で実行され、ノード単位のレポートが返されます。

## バインディング

「バインディング」はワークフローの入力キーを Manga Autopilot の
入力型にマッピングする定義です。

| 型          | 説明                                                |
|-------------|-----------------------------------------------------|
| `prompt`    | フリーテキストのプロンプト (ポジティブ)              |
| `negative`  | フリーテキストのネガティブプロンプト                |
| `seed`      | 整数シード (バインドされない場合は自動生成)         |
| `steps`     | ステップ数 (整数)                                   |
| `cfg`       | CFG 値 (浮動小数)                                   |
| `width`     | 画像幅 (整数)                                       |
| `height`    | 画像高さ (整数)                                     |
| `image`     | 参照画像パス                                        |
| `lora`      | LoRA 名 + 強度                                      |

バインディングはワークフロー JSON 内に記述します。

```json
{
  "id": "anime_t2i",
  "name": "Anime Text-to-Image",
  "type": "t2i",
  "definition": { ... },
  "bindings": [
    {"input_key": "6.inputs.text", "type": "prompt", "required": true},
    {"input_key": "6.inputs.seed", "type": "seed", "required": false}
  ]
}
```

## テストラン

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/workflows/anime_t2i/test-run \
  -H 'Content-Type: application/json' \
  -d '{"overrides": {"prompt": "1girl, blue hair, masterpiece"}}'
```

ランナーはオーバーライドを適用し、ローカルの ComfyUI サーバーにジョブを
投入します。HTTP レスポンスはディスパッチステータスを表す JSON で、
画像バイナリは ComfyUI 標準の `/view` エンドポイント経由で取得されます
(本 API からは返しません)。
