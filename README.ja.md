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
- **1 ページ v1.0 happy path** — `POST /projects` → `POST /autopilot/start`
  (`page_count=1`) で、デフォルトパイプラインを一通り完走する:
  `StoryPlanner` → `CharacterPlanner` → `PagePlanner` → `PanelPlanner` →
  `PromptBuilder` → `GenerationLoop` (候補生成 → executor 実行 → QA →
  リトライ → fallback) → `PageRenderer` → `ExportService` →
  `ManifestWriter`。 生成されたコマ画像は `assets/panels/`、ページ
  レンダーは `exports/pages/page_0001.png`、`manifest.json` +
  `generation_log.json` が書き出される。 fake LLM / fake executor
  を使う 1 ページ E2E テスト (`test_one_page_e2e.py`) がディスク上の
  成果物を検証する。
- **マルチページ / マルチパネル自動操縦** — `page_count` と
  `panels_per_page` パラメータで、1 ページに複数の `PanelRecord` を
  生成し、各パネルに個別の `GenerationJob`、`SpeechBubble`、
  fallback レイアウトを割り当てる。 ページは `export_page_png` で
  `exports/pages/page_NNNN.png` に書き出される。 E2E テストで
  1ページ/2パネル、1ページ/3パネル、2ページ/1パネル、
  4ページ/1パネル、4ページ/2パネルの構成を検証済み。
- **Project / Panel HTTP API** — `GET/POST /projects`、
  `GET/PATCH/DELETE /projects/{id}`、`GET /projects/_suggest_id` (spec
  §21.2) と `POST /panels/{id}/{generate,regenerate,repair}`、
  `PATCH /panels/{id}`、`GET /panels/{id}` (spec §21.6)。 生成
  エンドポイントは `GenerationJob` (status / candidates / 選択候補 /
  リトライ履歴) を `jobs/{job_id}.json` に永続化し、対象の
  `PanelRecord` の `image_path` と history を更新する。
- **Web 拡張** — サイドバータブ、プロジェクトピッカー、ページエディタ、
  キャラクターマネージャー、進捗モニター、書き出しセンターを
  `web/index.js` からマウント。
- **ComfyExecutor E2E** — フェイク `ComfyClient` + 実 `WorkflowRegistry` +
  実 `ComfyExecutor` 経路で、実 ComfyUI サーバーなしに
  `/prompt` → `/history` → `/view` のフローを E2E 検証。
  ワークフロー binding override（positive/negative/seed/width/height）が
  送信グラフに反映されることを確認（`test_comfy_executor_e2e.py`）。
- **実 ComfyUI E2E（opt-in）** — 環境変数で有効化するスモークテスト
  （`test_real_comfy_executor_e2e.py`）。デフォルトでは skip。
  `MANGA_AUTOPILOT_REAL_COMFY_E2E=1`、`MANGA_AUTOPILOT_COMFY_BASE_URL`、
  `MANGA_AUTOPILOT_TEST_WORKFLOW_JSON` を設定すると実行。
  ローカル/LAN/クラウド GPU の ComfyUI インスタンスに対応。
- **Webtoon + PDF Autopilot 書き出し** — ページ描画後に Autopilot の
  書き出し hook が Webtoon（フル + ページ別スライス）と PDF を生成する。
  `ManifestExports` には `webtoon`（PNG パスのリスト）と `pdf`
  （`manga.pdf` のパス）が含まれる。 E2E テストでディスク上の
  ファイルと manifest への反映を検証。
- **プロジェクト再編集 E2E** — 生成済みプロジェクトを新しい app
  インスタンスから再読み込みし、`PATCH /bubbles/{id}` で吹き出し
  テキストを編集、ページ PNG / Webtoon / PDF を再レンダリング・
  再出力できることを E2E 検証（`test_project_reedit_e2e.py`）。
  HTTP API による台詞編集・Web UI 編集は未接続。
- **Autopilot 失敗→再開 E2E** — パネル生成中にエクゼキュータ障害が
  発生した場合、パイプラインは `FAILED_PANEL_GENERATION` に遷移し
  `generation_log.json` を書き出す。再開時（`POST .../autopilot/start`）、
  冪等な `generate_panels` hook は既生成パネルをスキップし未生成分のみ
  生成する。`test_autopilot_resume_e2e.py` で fail → resume → complete
  のラウンドトリップとアーティファクト検証を E2E 検証。
- **プロジェクトバンドル import E2E** — 生成済みプロジェクトを
  `ExportService.zip()` で ZIP エクスポートし、別の `storage_root` に
  `ExportService.import_zip()` でインポートしても、再読み込み・
  編集・再出力できることを E2E 検証（`test_project_bundle_import_e2e.py`）。
  generate → ZIP export → 別 storage への import → 新 app での
  取得 → bubble テキスト編集 → テキスト変更後の再レンダリング
  （ハッシュ変化検証） → Webtoon/PDF 再出力 → manifest 再構築 →
  全アーティファクト整合性確認。

### Planned (未接続 / スタブ)

- **Web UI 編集。** プロジェクト・パネル・吹き出し・台詞のブラウザ
  上でのフル編集。現在は `PATCH /bubbles/{id}` HTTP API で吹き出し
  テキストの編集が可能だが、ビジュアルエディタは未実装。
- **履歴付き編集 UI。** Undo/Redo とパネル単位のリビジョン管理。
- **差分プレビュー。** 編集後の再レンダリングで Before/After を並べて
  比較。
- **複雑なコマ割り AI。** 現在のグリッド/fallback レイアウトを超える
  AI ベースのコマ構成。
- **ZIP import 競合解決 UI。** 既存プロジェクトと重複するバンドルを
  インポート時のマージ・上書き・スキップ UI。
- **外部 GPU worker (Modal 風) のエンドツーエンド接続。** `GPUBridge` は
  ワークフローのシリアライズとローカル ComfyUI フォールバックを実装済み
  だが、デフォルト Orchestrator からはまだ使われていない。
- **通常 CI での実 ComfyUI 必須化。** 標準 CI でライブ ComfyUI
  サーバーを必須に（現在は opt-in のみ）。
- **実画像 QA スコアリング。** 現在はヒューリスティック (プロンプト整合性、
  吹き出しスペース、パレット) を使っている。 CLIP / IP-Adapter / 顔
  類似度スコアリングはロードマップ上の作業。`GenerationLoop` は既に
  チェッカーを呼び出して失敗時に再生成するので、チェッカーを差し替える
  だけで周辺のコードはそのまま動く。

各フェーズの詳細ステータスは `docs/comfyui_manga_autopilot_spec.md`
§30-§42 を参照。

### Opt-in 実 ComfyUI E2E

`test_real_comfy_executor_e2e.py` は**デフォルトで skip** され、
3 つの環境変数が設定されている場合だけ実行されます。これにより、
標準の `pytest tests/backend/ -q` は GPU 不要で高速に保ちつつ、
実 ComfyUI サーバーを持つ開発者は full executor パスを検証できます。

```bash
# 標準テストスイート（GPU 不要）:
pytest tests/backend/ -q

# Opt-in 実 ComfyUI E2E（実 ComfyUI サーバーが必要）:
MANGA_AUTOPILOT_REAL_COMFY_E2E=1 \
MANGA_AUTOPILOT_COMFY_BASE_URL=http://192.168.1.50:8188 \
MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=/path/to/workflow_api.json \
pytest tests/backend/test_real_comfy_executor_e2e.py -q
```

| 環境変数 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `MANGA_AUTOPILOT_REAL_COMFY_E2E` | Yes | `0` | `1` でテスト有効化 |
| `MANGA_AUTOPILOT_COMFY_BASE_URL` | Yes | — | ComfyUI サーバー URL（ローカル/LAN/クラウド） |
| `MANGA_AUTOPILOT_TEST_WORKFLOW_JSON` | Yes | — | `api_graph` + `bindings` を持つ workflow JSON のパス |
| `MANGA_AUTOPILOT_REAL_COMFY_TIMEOUT` | No | `180` | autopilot 完了待ちの最大秒数 |

**注意:**
- workflow JSON は対象 ComfyUI サーバーに存在するモデル/ノードを
  参照している必要があります。
- 低スペック開発PC: 標準テストスイートのみ実行（GPU 不要）。
- GPU 搭載 PC / リモート環境: `COMFY_BASE_URL` を ComfyUI インスタンスに
  向けて opt-in テストを実行。

### v1.0 リリースゲート

リリース前に必ず以下の品質ゲートを実行してください:

```bash
# リント
ruff check .

# フル バックエンドテストスイート
pytest tests/backend/ -q

# リリースゲート サブセット（主要 E2E のみ）
pytest -m release_gate -q
```

`release_gate` マーカーは以下の主要 E2E パスを対象とします:

| テスト | 検証内容 |
|--------|----------|
| `test_one_page_autopilot_completes_end_to_end` | 1ページ × 1コマ生成 |
| `test_four_page_two_panel_autopilot_completes_end_to_end` | 4ページ × 2コマ + Webtoon/PDF |
| `test_one_page_autopilot_uses_comfy_executor_path` | ComfyExecutor + FakeComfyClient |
| `test_generated_project_can_be_reopened_edited_and_reexported` | プロジェクト再編集ラウンドトリップ |
| `test_failed_autopilot_can_resume_missing_panels_only` | 失敗 → 再開 → 完了 |
| `test_generated_project_bundle_can_be_imported_edited_and_reexported` | ZIP バンドル import ラウンドトリップ |

詳細は [`docs/release/v1_acceptance_matrix.md`](docs/release/v1_acceptance_matrix.md)
を参照してください。

## 特徴

- **プロジェクト + ストーリー構成** (LLM 駆動、JSON 修復機能付き)
- **キャラクターマネージャー** - 参照画像アップロード、IP-Adapter、LoRA
  バインディング
- **ワークフローレジストリ** - ライブの ComfyUI `object_info` に対する
  スキーマ検証とワンクリックのテストラン
- **ページ / コマエディタ** - テンプレートベースのレイアウトと SVG/PNG
  レンダリング
- **吹き出し** - 自動配置、縦書き日本語対応、PNG 出力、
  Autopilot レタリングフックによるレンダリング済みページへの吹き出しオーバーレイ
- **候補生成** - シードポリシーによる複数候補 / **QA スコアリング** /
  リトライプロンプト (spec 17-18)
- **Autopilot ステートマシン** - 一時停止 / 再開 / キャンセル、
  リカバリ戦略 (spec 7)
- **キャンセル API 基盤** - `POST /autopilot/cancel` で `cancel.json`
  マーカーを保存; `GenerationLoop` が候補間でマーカーを確認;
  `RemoteHTTPExecutor.cancel()` でリモートジョブにキャンセル送信;
  キャンセルされたジョブは `RemoteExecutorCancelledError` を送出
- **キャンセル後プロジェクト再開** - `POST /autopilot/restart` で
  キャンセルマーカーを削除し、`project.json` から入力を復元して
  新しい実行を開始; リクエストボディで上書き可能
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

## ローカルスモークテスト

インストール後、以下のコマンドで動作を確認できます:

```bash
python -m pip install -e .
ruff check .
pytest -m release_gate -q
```

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

## サンプル / スターターキット

`examples/` にすぐに使えるワークフロー例を同梱しています:

- [`examples/workflows/anime_t2i_default.registry.json`](examples/workflows/anime_t2i_default.registry.json) —
  WorkflowRegistry ペイロード（`/workflows` にコピーするか API 経由で登録）
- [`examples/workflows/anime_t2i_default.workflow.json`](examples/workflows/anime_t2i_default.workflow.json) —
  ComfyUI API 形式ワークフローグラフ
- [`examples/workflows/README.md`](examples/workflows/README.md) —
  ステップバイステップの適用手順

## リモートエグゼキュータ基盤

リモートエグゼキュータパスにより、パネル生成を外部の HTTP GPU ワーカー
（Modal、RunPod、リモート ComfyUI サーバーなど）に委任できます。
現時点では:

- `RemoteHTTPExecutor` が `GenerationExecutor` プロトコルを実装
- **同期**（単一 POST）と**非同期**（POST → job_id → GET poll）の
  両方のモードに対応
- 型付き例外: `RemoteExecutorHTTPError`、`RemoteExecutorTimeoutError`、
  `RemoteExecutorResponseError`、`RemoteExecutorImageError`、
  `RemoteExecutorPollingTimeoutError`、`RemoteExecutorJobError`
- リモートワーカー失敗時に Autopilot が `FAILED` 状態になり、
  `generation_log.json` に失敗理由が記録される
- CI テスト用の `FakeRemoteWorker` インプロセスサーバーを同梱
- Modal / RunPod / 外部 GPU との本番連携は**まだ未実装**
- 標準 CI で外部ネットワークサービスは不要

エグゼキュータの切り替えは aiohttp app 上の `manga_panel_executor` または
`manga_panel_executor_factory` で設定します。
詳細は `tests/backend/test_remote_executor_e2e.py` を参照してください。

エグゼキュータは3つの画像配信形式をサポートしています:

- **`image_base64`**: インライン base64（MVP デフォルト）
- **`artifact_url`**: 画像ダウンロード用 URL（大きな画像に推奨；将来の S3 / R2 / Modal Volume 連携向け）
- **`artifact_path`**: ローカルファイルパス（ローカル/テスト用ワーカー向け）

CI ではインメモリストレージを持つ `FakeRemoteWorker` を使用しています。

## エグゼキュータ インターフェース

エグゼキュータ インターフェースは `PanelExecutionRequest` を使用して、
構造化されたパネル実行コンテキストを渡します:

- `project_id`, `page_id`, `panel_id`, `candidate_id`
- `prompt`, `workflow_id`, `seed`, `attempt_index`
- `width`, `height`, `output_filename`, `metadata`

これにより、Modal / S3 / R2 連携のための堅牢なアーティファクト命名が可能になります。
リモートワーカーは project/page/panel/candidate のコンテキストを受け取ります。

## Modal ワーカー MVP

実験的な Modal ワーカーの例を `examples/modal-worker/` に用意しています。
将来の Modal GPU 実行のためのスケルトンであり、CI で Modal アカウントや
GPU は不要です。

- Modal SDK なしでも動作する純 Python ヘルパーを同梱
- `RemoteHTTPExecutor` contract に準拠
- 現時点では ComfyUI やモデル実行は未実装
- 詳細は `examples/modal-worker/README.md` を参照

## ドキュメント

- [`CHANGELOG.md`](CHANGELOG.md) — リリース履歴
- [`docs/release/v1_acceptance_matrix.md`](docs/release/v1_acceptance_matrix.md) —
  v1.0 受入基準マトリクス
- [`docs/release/v1_release_checklist.md`](docs/release/v1_release_checklist.md) —
  リリース前チェックリスト
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
