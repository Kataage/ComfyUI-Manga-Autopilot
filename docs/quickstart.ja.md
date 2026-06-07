# クイックスタート

> 🌐 **言語 / Language:** [English](quickstart.md) | **日本語** (このファイル)

ComfyUI Manga Autopilot の最短ハッピーパスです。

## 1. ComfyUI を起動

```bash
python main.py --listen 0.0.0.0 --port 8188
```

## 2. カスタムノードをインストール

[`docs/install.ja.md`](docs/install.ja.md) の手順に従ってください。
インストール後に ComfyUI を再起動します。

## 3. Manga Autopilot タブを開く

ComfyUI のサイドバーに新しく追加された **Manga Autopilot** タブを
クリックします。

## 4. サンプルワークフローを登録

**Workflows → Register workflow** をクリックし、`workflows/` 配下の
サンプルファイル (例: `anime_t2i_api.json`) を選択します。
ComfyUI サーバーに同じノードがインストールされていれば、検証は
自動で成功します。

## 5. サンプルプロジェクトを作成

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
        "project_id": "demo",
        "title": "Demo",
        "idea": "A hero receives a black sword",
        "page_count": 4,
        "format": ["png_pages"]
      }'
```

## 6. PNG を書き出す

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/projects/demo/export/png \
  -H 'Content-Type: application/json' \
  -d '{"pages": {"page_1": [{"panel_id": "p1", "x": 16, "y": 16, "width": 600, "height": 400}]}}'
```

出力ファイルは
`{storage_root}/projects/demo/exports/pages/page_0001.png` に書き出されます。

## 次のステップ

- [`docs/workflow_binding.ja.md`](docs/workflow_binding.ja.md) -
  自前のワークフローを登録して入力をバインドする
- [`docs/character_consistency.ja.md`](docs/character_consistency.ja.md) -
  参照画像 / IP-Adapter / LoRA を設定する
- `docs/autopilot.md` - LLM 駆動のパイプラインをエンドツーエンドで実行する
