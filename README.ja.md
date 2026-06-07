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

## ステータス (v0.x → v1.0.0)

このセクションは「今日、何が実際に動くのか」の唯一の正解です。
機能リストは **Implemented** (テストと HTTP API でカバー済み) と
**Planned** (仕様書には記載済みだが、未接続 / スタブ) に分けています。

### Implemented (実装済み)

- **HTTP API** — `/manga_autopilot/api/{health,workflows,…}` を ComfyUI の
  `PromptServer` Application に登録。 各ハンドラは Application コンテキスト
  の `manga_storage_root` / `manga_workflow_registry` を解決するので、
  ComfyUI 起動後に character / export / workflow ルートが 500 にならない。
- **ワークフローレジストリ** — 登録、一覧、取得、更新、削除。タイプ別の
  必須 binding 検証 (`text_to_image` は `positive_prompt/negative_prompt/
  seed/width/height` + `output_node` または `filename_prefix` の片方が必須;
  `upscale` は `reference_image` が必須; `reference_to_image` /
  `image_to_image` は `reference_image` を追加; など)。
- **ワークフローのテストラン** — `/workflows/{id}/test-run` は
  `ComfyClient.submit_workflow` 経由で ComfyUI に投入し、`/history/{id}`
  をポーリングし、`/view` で画像を取得して
  `{storage_root}/test_runs/{workflow_id}/` に保存する。
- **ComfyClient** — `/prompt`、`/history/{id}`、`/view`、`/upload/image`、
  `/object_info`、`/system_stats`、`/devices`、`/extensions`、WebSocket
  `/ws` の全トランスポート層。
- **プロジェクトストレージレイアウト** — `ensure_storage_root` /
  `ensure_project_paths` が仕様書 §9.1 のディレクトリを作成する。
- **キャラクターサービス** — CRUD、参照画像アップロード(サイズ / 拡張子
  検証)、`build_character_prompt`、IP-Adapter / LoRA オーバーライド、
  シートビューヘルパー、キャラクタカード書き出し。
- **吹き出しサービス** — CRUD、分類器による回転付き自動配置、PNG
  レンダリング。
- **ページレンダラー** — コマ枠 **および** 生成画像を 1 枚のページ PNG に
  合成 (`cover` / `contain` / `stretch` のフィットモード、任意回転)。
- **書き出しサービス** — PNG ページ、Webtoon 連結 + スライス、
  PDF (A4/B5/Kindle/custom、余白、DPI)、プロジェクトバンドラー。
- **書き出しパスの安全性** — `ExportService.resolve_page_pngs` は
  プロジェクトストレージツリー外のファイルを拒否する。
- **プロジェクトインポーター** — 安全な zip 展開 (Zip Slip 対策: 絶対パス
  と `..` セグメントを拒否)。
- **Autopilot** — 16 ステート + 8 失敗ステートの状態機械、エラーリカバリ
  テーブル、`AutopilotController` (pause / resume / cancel)、各ステップ
  をストーリー → ページ → コマ → プロンプト → ワークフロー → コマ生成 →
  QA → レタリング → ページ描画 → 書き出し → finalize の順に進める
  `Orchestrator` (各ステップは注入可能なフック)。
  HTTP `/autopilot/{start,pause,resume,cancel,status}` はバックグラウンドの
  `asyncio.Task` で Orchestrator を起動する。Orchestrator は **各ステップ
  開始時に** `asyncio.Event` を `await` して pause を尊重する。`resume` を
  呼ぶと状態機械を pause 直前のステートに巻き戻し、ブロックを解除する。
  ステップ **実行中** の即時 pause は自動では効かないため、長時間走る
  hook は必要に応じて `run.pause_event` を自分自身で監視すること。
- **Web 拡張** — サイドバータブ、プロジェクトピッカー、ページエディタ、
  キャラクターマネージャー、進捗モニター、書き出しセンターを
  `web/index.js` からマウント。

### Planned (未接続 / スタブ)

- **LLM 駆動の story / character / page / panel planner のフル接続。**
  デフォルトの Orchestrator フックは対応するサービスを空 / プレースホルダ
  入力で呼ぶ。プランナー本体は実装 + ユニットテスト済みだが、デフォルトの
  プロジェクトブートストラップはまだ LLM 応答を渡していない。配線は別
  イシューで追跡。
- **外部 GPU worker (Modal 風) のエンドツーエンド接続。** `GPUBridge` は
  ワークフローのシリアライズとローカル ComfyUI フォールバックを実装済み
  だが、デフォルト Orchestrator からはまだ使われていない。
- **画像品質 / プロンプト整合性 / キャラクター一貫性チェッカー。**
  QA + リトライサービスにはフックはあるが、各チェッカーのスコアは定数を
 返している。 実際の CLIP / IP-Adapter / 顔類似度スコアリングはロードマップ
  上の作業。

各フェーズの詳細ステータスは `docs/comfyui_manga_autopilot_spec.md`
§30-§42 を参照。

## 特徴

- **プロジェクト + ストーリー構成** (LLM 駆動、JSON 修復機能付き)
- **キャラクターマネージャー** - 参照画像アップロード、IP-Adapter、LoRA
  バインディング
- **ワークフローレジストリ** - ライブの ComfyUI `object_info` に対する
  スキーマ検証とワンクリックのテストラン
- **ページ / コマエディタ** - テンプレートベースのレイアウトと SVG/PNG
  レンダリング
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
- [`docs/character_consistency.ja.md`](docs/character_consistency.ja.md) -
  キャラクター一貫性
- [`docs/troubleshooting.ja.md`](docs/troubleshooting.ja.md) - トラブルシューティング
- [`docs/contribution.ja.md`](docs/contribution.ja.md) - コントリビューション
- [`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md) -
  仕様書

## ライセンス

本プロジェクトは [LICENSE](LICENSE) の条項でライセンスされます。
同梱のサンプルワークフローも同じライセンスで配布されますが、
そこで参照されているモデルファイルは本リポジトリには含まれていません。
