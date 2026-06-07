# ComfyUI Manga Autopilot

> 🌐 **言語 / Language:** [English](README.md) | **日本語** (このファイル)

ComfyUI のカスタムノード拡張で、1 つのアイデアから短い漫画 / Webtoon 作品を
エンドツーエンドで自動生成するパイプラインを提供します。

- ストーリー構成
- キャラクター設計
- コマ割り
- 画像生成
- 品質チェック
- セリフ / 吹き出し
- 書き出し

すべてを ComfyUI の中で完結できます。

本リポジトリは
[`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md)
(v1.0.0) の設計に準拠しています。

## 特徴

- **プロジェクト + ストーリー構成** (LLM 駆動、JSON 修復機能付き)
- **キャラクターマネージャー** - 参照画像アップロード、IP-Adapter、LoRA バインディング
- **ワークフローレジストリ** - ライブの ComfyUI `object_info` に対するスキーマ検証と
  ワンクリックのテストラン
- **ページ / コマエディタ** - テンプレートベースのレイアウトと SVG/PNG レンダリング
- **吹き出し** - 自動配置、縦書き日本語対応、PNG 出力
- **候補生成** - シードポリシーによる複数候補 / **QA スコアリング** /
  リトライプロンプト (spec 17-18)
- **Autopilot ステートマシン** - 一時停止 / 再開 / キャンセル、
  リカバリ戦略 (spec 7)
- **書き出し** - PNG ページ、Webtoon スライス、PDF (A4/B5/Kindle/カスタム)、
  Zip 形式のプロジェクトバンドル
- **外部 GPU ブリッジ** (Modal スタイル worker) - タイムアウトでローカル
  ComfyUI にフォールバック

## 必要環境

- Python 3.10 以上
- ComfyUI 0.3.x 以降 (本カスタムノードは API 形式のワークフローを扱います)
- Pillow 10 以上
- `aiohttp` 3.9 以上
- `pydantic` 2 以上
- `jsonschema` 4 以上

## インストール

詳細は [`docs/install.ja.md`](docs/install.ja.md) を参照してください。

```bash
# ComfyUI インストール先ディレクトリで
cd custom_nodes
git clone https://github.com/Kataage/ComfyUI-Manga-Autopilot
cd ComfyUI-Manga-Autopilot
pip install -e .
```

ComfyUI を再起動すると、UI に「Manga Autopilot」タブが表示されます。

## クイックスタート

詳細は [`docs/quickstart.ja.md`](docs/quickstart.ja.md) を参照してください。

```text
1. ComfyUI を起動
2. custom_nodes に本リポジトリを配置
3. 依存関係をインストール
4. Manga Autopilot タブを開く
5. サンプルワークフロー (workflows/anime_t2i_api.json) を登録
6. サンプルプロジェクトを作成
7. 「Export PNG」を実行
```

## サンプルワークフロー

`workflows/` ディレクトリにすぐに登録できる API 形式のワークフローを
5 本同梱しています。

- `anime_t2i_api.json`
- `anime_i2i_api.json`
- `anime_reference_api.json`
- `character_sheet_api.json`
- `upscale_api.json`

## ドキュメント

- [`docs/quickstart.ja.md`](docs/quickstart.ja.md) - 最短手順
- [`docs/install.ja.md`](docs/install.ja.md) - インストール
- [`docs/workflow_binding.ja.md`](docs/workflow_binding.ja.md) - ワークフロー登録
- [`docs/character_consistency.ja.md`](docs/character_consistency.ja.md) - キャラクター一貫性
- [`docs/troubleshooting.ja.md`](docs/troubleshooting.ja.md) - トラブルシューティング
- [`docs/contribution.ja.md`](docs/contribution.ja.md) - コントリビューション
- [`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md) - 仕様書

## ステータス

v1.0.0 リリース前につき、公開 API とフォルダ構成は変更される可能性があります。
実装は上記の仕様に対してイシュー単位で進めています。

## ライセンス

本プロジェクトは [LICENSE](LICENSE) の条項でライセンスされます。
同梱のサンプルワークフローも同じライセンスで配布されますが、
そこで参照されているモデルファイルは本リポジトリには含まれていません。
