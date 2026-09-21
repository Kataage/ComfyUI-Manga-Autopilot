# インストール

> 🌐 **言語 / Language:** [English](install.md) | **日本語** (このファイル)

**ComfyUI Manga Autopilot** を ComfyUI カスタムノードとして
インストールする手順を説明します。

## 1. 前提条件

- Python 3.10 以上
- 動作する [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 環境
- 同梱のサンプルワークフローが参照するモデルファイル
  (ComfyUI の `models/` ディレクトリに配置)
- LLM エンドポイント (任意)
  - プランナーを LLM 駆動で使う場合のみ必要
  - Ollama / OpenAI 互換 / `manual` プロバイダ (手入力) に対応

## 2. クローン

```bash
cd path/to/ComfyUI/custom_nodes
git clone https://github.com/Kataage/ComfyUI-Manga-Autopilot
cd ComfyUI-Manga-Autopilot
```

## 3. 依存関係のインストール

```bash
pip install -e .
```

`aiohttp`、`pydantic`、`PyYAML`、`Pillow`、`jsonschema` が自動で
インストールされます。開発作業をする場合は dev extras も追加してください。

```bash
pip install -e ".[dev]"
```

**`PATH` 上の `pip` ではなく、ComfyUI 自身のインタプリタを使ってください。**
ポータブル版やデスクトップ版の ComfyUI は専用の Python を同梱しており、別の
Python に入れても ComfyUI からは見えません。ComfyUI には `aiohttp`・`pydantic`・
`PyYAML`・`Pillow` は既にありますが、`jsonschema` はありません。これが無いと
起動時に `register_all()` が `ModuleNotFoundError` を送出します。その状態でも
サイドバータブは表示されるため、インストール済みに見えて HTTP ルートが全滅
します。ComfyUI のコンソールに `Failed to register Manga Autopilot routes` が
出ていないか確認してください。

```bash
# 例: Windows の ComfyUI Desktop
"%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs\<name>\standalone-env\python.exe" -m pip install -e .
```

ComfyUI-Manager 経由でインストールする場合は `requirements.txt` が使われ、
同じ依存関係が入ります。

## 4. ComfyUI の再起動

カスタムノードは `/manga_autopilot/api/...` 配下の HTTP API を登録し、
ComfyUI の Web UI に「Manga Autopilot」タブを追加します。

## 5. 動作確認

```bash
curl http://localhost:8188/manga_autopilot/api/health
# -> {"status": "ok"}
```

## 6. 任意: 外部 GPU worker

`docs/modal_bridge.md` (または
`src/manga_autopilot/services/gpu_bridge.py`) を参照してください。
ブリッジはオプトインで、`worker.endpoint` を空のままにしておくと
ローカル生成にフォールバックします。

## トラブルシューティング

ルートが登録されない場合は以下を確認してください。

- `WEB_DIRECTORY` がこのリポジトリの `web/` を指していること
- ComfyUI のコンソールに `manga_autopilot.routes` 関連のエラーが出ていないこと
- ComfyUI の venv で `python -c "import manga_autopilot"` が成功すること

詳細は [`docs/troubleshooting.ja.md`](docs/troubleshooting.ja.md) を参照してください。
