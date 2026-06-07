# ComfyUI Manga Autopilot 完全仕様書

**版数**: v1.0.0  
**作成日**: 2026-06-07  
**対象プロジェクト**: ComfyUIを基盤にした、Anifusion風AI漫画・Webtoon自動生成OSS  
**目的**: このMarkdownファイルのみを読めば、初参加の開発者・AIエージェント・設計者が、全体要件、設計思想、実装方針、データ構造、API、画面、ワークフロー、Issue分割、テスト観点まで理解し、すぐに実装に参加できる状態にする。

---

## 0. この仕様書の読み方

この仕様書は、単なるアイデアメモではなく、**開発開始可能な実装仕様書**として書く。  
最初に全体像を理解し、その後、実装担当ごとに必要な章を読めばよい。

### 0.1 想定読者

| 読者 | 読むべき章 |
|---|---|
| プロジェクト責任者 | 1〜7、31〜36 |
| バックエンド担当 | 8〜18、21〜24、27、30 |
| フロントエンド担当 | 19〜20、25、30 |
| ComfyUI連携担当 | 10〜14、21、24 |
| 画像生成ワークフロー担当 | 12〜17、22 |
| LLM/自動化担当 | 9、15、16、23 |
| QA/テスト担当 | 17、28〜30、35 |
| OSS運営担当 | 31〜36 |
| AIエージェント実装担当 | 全章、とくに 7、18、23、32 |

### 0.2 この仕様書で決めること

- 作るものの定義
- 作らないものの定義
- MVPから最終形までの段階
- ComfyUIとの接続方式
- 自動完走パイプライン
- キャラクター固定戦略
- コマ割り、吹き出し、セリフ配置仕様
- データモデル
- API仕様
- 画面仕様
- フォルダ構成
- 実装順序
- GitHub Issue分割
- テスト・受け入れ条件
- OSS公開・ライセンス方針

---

## 1. プロジェクト概要

### 1.1 プロジェクト名

仮称：**ComfyUI Manga Autopilot**

候補名：

- ComfyUI Manga Autopilot
- ComfyUI Comic Studio
- ComfyUI Webtoon Studio
- Manga Autopilot for ComfyUI
- Story2Manga for ComfyUI
- ComfyUI Manga Flow

本仕様書では、以後 **Manga Autopilot** と呼ぶ。

### 1.2 一文定義

**Manga Autopilot は、短い企画文から、ストーリー、キャラクター、コマ割り、画像生成、品質チェック、再生成、吹き出し、セリフ配置、Webtoon/PDF/PNG出力までを自動完走する ComfyUI 拡張型OSSである。**

### 1.3 なぜ作るのか

既存の画像生成ツールは、1枚絵の生成には強い。  
しかし、漫画制作では以下の工程が分断されている。

1. ストーリー作成
2. キャラクター設定
3. キャラクター固定
4. ネーム作成
5. コマ割り
6. 各コマのプロンプト作成
7. 画像生成
8. 失敗コマの再生成
9. 吹き出し配置
10. セリフ入れ
11. ページ画像化
12. Webtoon/PDF出力

Manga Autopilot は、この制作工程全体をパイプライン化し、**漫画制作の自動運転**を実現する。

### 1.4 目指す最終体験

ユーザーが以下のように入力する。

```txt
ジャンル: ダークファンタジー
ページ数: 8ページ
主人公: 銀髪の少女
雰囲気: 暗く重いが、最後は熱い
出力形式: Webtoon + PDF
```

システムは、以下を自動で作成する。

- 作品タイトル
- あらすじ
- 登場キャラクター
- キャラクター設定
- キャラクター参照画像
- ページ構成
- コマ構成
- コマごとのプロンプト
- コマ画像
- 品質チェック結果
- 自動再生成結果
- 吹き出し
- セリフ
- 擬音
- 完成ページPNG
- Webtoon縦長画像
- PDF
- 再編集可能なプロジェクトJSON

---

## 2. 背景と前提

### 2.1 ComfyUIを基盤にする理由

Manga Autopilot は、独自の画像生成エンジンを作らない。  
生成処理は ComfyUI に委譲する。

理由：

- 既存のComfyUIワークフロー資産を使える
- モデル、LoRA、ControlNet、IP-Adapter、Inpaint、Upscaleなどを流用できる
- ComfyUI APIから外部制御できる
- ComfyUI拡張としてUIを組み込める
- ローカル実行と外部GPU実行の両方に対応しやすい
- OSSユーザーに受け入れられやすい

### 2.2 ComfyUI公式仕様上の前提

ComfyUIはクライアント/サーバーモデルで動作する。  
サーバー側はPython、クライアント側はJavaScriptで、APIモードでは外部UIやCLIからワークフローを送信できる。

本仕様では、以下のComfyUI機能を利用する。

| ComfyUI機能 | 用途 |
|---|---|
| `/prompt` | API形式ワークフローの投入 |
| `/history/{prompt_id}` | 生成結果の取得 |
| `/view` | 画像ファイルの取得 |
| `/upload/image` | 参照画像・入力画像のアップロード |
| `/object_info` | ノード定義の取得・ワークフロー検証 |
| `/ws` | 生成進捗のリアルタイム監視 |
| `WEB_DIRECTORY` | ComfyUI拡張UIのJavaScript読み込み |
| `app.registerExtension` | ComfyUIクライアントUIへの拡張登録 |

### 2.3 このプロジェクトの本質

このプロジェクトの本質は、画像生成モデルを作ることではない。

**漫画制作ワークフローを、ComfyUIを使って自動化することである。**

価値の中心：

```txt
ストーリー
  ↓
キャラ設計
  ↓
キャラ固定
  ↓
ネーム
  ↓
コマ割り
  ↓
画像生成
  ↓
品質チェック
  ↓
自動再生成
  ↓
吹き出し/セリフ
  ↓
完成出力
```

---

## 3. スコープ

### 3.1 作るもの

Manga Autopilot が提供する機能は以下。

| 区分 | 機能 |
|---|---|
| プロジェクト管理 | 漫画プロジェクト作成、保存、再編集、エクスポート |
| 企画入力 | 一文企画、ジャンル、ページ数、絵柄、出力形式の入力 |
| ストーリー生成 | あらすじ、章構成、ページ構成、コマ構成の自動生成 |
| キャラ管理 | キャラクター設定、見た目、服装、参照画像、LoRA設定 |
| キャラ固定 | キャラシート、参照画像、IP-Adapter/LoRA連携、プロンプト固定 |
| ワークフロー管理 | ComfyUI API形式ワークフロー登録、ノードバインド、検証 |
| コマ生成 | コマごとのプロンプト生成、候補生成、採用画像選定 |
| 品質チェック | 顔、手、キャラ一致、構図、余白、ノイズの自動評価 |
| 自動修復 | 失敗コマの再生成、プロンプト修正、ワークフロー変更 |
| 漫画編集 | ページ、コマ、吹き出し、セリフ、擬音の編集 |
| 自動レタリング | 吹き出し配置、縦書き/横書き、文字折返し |
| 出力 | PNGページ、Webtoon縦長、PDF、編集用JSON |
| 外部GPU | Modal等の外部GPUワーカーに生成だけ委譲する機構 |
| OSS運用 | GitHub公開、Issueテンプレ、Contribution Guide |

### 3.2 作らないもの

v1.0では以下を作らない。

- 独自Diffusionモデル
- 独自LoRA学習基盤
- Photoshop級の画像編集機能
- 完全なCLIP Studio代替
- 商用SaaS課金機能
- アカウント課金・決済機能
- 成人向け専用機能
- 著作権侵害を助長するキャラ模倣専用機能
- 外部サービスへの永続アップロード前提の設計

### 3.3 例外的に後回しにするもの

| 機能 | v1.0扱い | 理由 |
|---|---|---|
| 動画化 | 後回し | まず静止画漫画に集中する |
| 音声付き漫画 | 後回し | Webtoon完成後の拡張でよい |
| 複数人共同編集 | 後回し | ローカルOSSの初期価値から外れる |
| 高度な権限管理 | 後回し | SaaSではないため不要 |
| 自動投稿 | 後回し | Pixiv/Patreon/X連携は別モジュール化 |

---

## 4. 成功条件

### 4.1 MVP成功条件

MVPでは、以下ができれば成功とする。

1. ComfyUI内に Manga Autopilot のUIが表示される
2. プロジェクトを作成できる
3. 4コマまたはWebtoon短編を作成できる
4. 各コマにプロンプトとセリフを入力できる
5. ComfyUI APIで画像生成できる
6. 生成画像をコマに自動配置できる
7. 吹き出しとセリフを後乗せできる
8. PNGとして出力できる
9. プロジェクトJSONを保存・再読み込みできる

### 4.2 v1.0成功条件

v1.0では、以下ができれば成功とする。

1. 企画文からストーリーを自動生成できる
2. キャラクターを自動定義できる
3. キャラクター参照画像を生成できる
4. ページ構成とコマ構成を自動生成できる
5. 全コマの画像をComfyUIで自動生成できる
6. 複数候補から自動採用できる
7. 品質NG時に自動再生成できる
8. 吹き出しとセリフを自動配置できる
9. Webtoon縦長画像を出力できる
10. PDFを出力できる
11. 途中失敗しても再開できる
12. 初参加の開発者がIssue単位で実装できる

### 4.3 品質目標

| 指標 | 目標 |
|---|---|
| 4ページ漫画の完走率 | 80%以上 |
| 8ページ漫画の完走率 | 60%以上 |
| 1コマあたり自動採用率 | 70%以上 |
| 失敗コマの自動修復成功率 | 50%以上 |
| プロジェクト再読み込み成功率 | 100% |
| 出力ファイル生成成功率 | 95%以上 |
| UI操作なしの完全自動完走 | v1.0で対応 |

---

## 5. 用語定義

| 用語 | 定義 |
|---|---|
| プロジェクト | 1つの漫画制作単位 |
| ストーリー | 作品全体の構成 |
| ページ | 漫画ページまたはWebtoon上の論理ページ |
| コマ | 1つの漫画パネル |
| パネル | コマと同義。ただしデータモデル上はPanelと呼ぶ |
| キャラシート | 同一キャラの正面・横・表情・服装などの参照画像 |
| 参照画像 | IP-Adapter等で使うキャラ固定用画像 |
| ワークフロー | ComfyUI API形式JSON |
| バインド | Manga Autopilotの項目をComfyUIノード入力へ対応付けること |
| 候補画像 | 1コマに対して複数生成された画像 |
| QA | 自動品質チェック |
| レタリング | 吹き出し、セリフ、擬音を配置する工程 |
| Autopilot | 企画入力から完成出力まで自動完走する機能 |

---

## 6. 全体アーキテクチャ

### 6.1 基本構成

```txt
[User Browser]
  ↓
[ComfyUI Web Client]
  ↓
[Manga Autopilot UI Extension]
  ↓
[Manga Autopilot Backend]
  ↓
[ComfyUI Server API]
  ↓
[ComfyUI Workflow Execution]
  ↓
[Local Storage / Optional GPU Worker]
```

### 6.2 コンポーネント一覧

| コンポーネント | 責務 |
|---|---|
| Frontend UI | 入力、編集、プレビュー、進捗表示 |
| Project Manager | プロジェクト保存、読み込み、状態管理 |
| Story Planner | 企画からストーリー・ページ・コマ構成を生成 |
| Character Manager | キャラクター設定、参照画像、LoRA管理 |
| Prompt Builder | コマ構成を画像生成プロンプトへ変換 |
| Workflow Binder | ワークフローJSONと入力項目の対応付け |
| ComfyUI Client | ComfyUI API呼び出し |
| Job Queue | コマ生成ジョブの管理 |
| Quality Checker | 画像品質チェック |
| Retry Controller | 失敗時の再生成制御 |
| Lettering Engine | 吹き出し、セリフ、擬音配置 |
| Renderer | ページ画像/Webtoon/PDF生成 |
| Modal Bridge | 外部GPU実行との接続 |
| Asset Cleaner | 一時ファイル削除、ログ整理 |

### 6.3 実行モード

#### 6.3.1 ローカルモード

すべてローカルPCで完結する。

```txt
Browser
  ↓
ComfyUI Extension UI
  ↓
Local Manga Backend
  ↓
Local ComfyUI
  ↓
Local Disk
```

メリット：

- 生成画像が外部へ出ない
- 既存ComfyUI環境を使える
- 無料運用しやすい
- OSSとして導入しやすい

#### 6.3.2 外部GPUブリッジモード

生成だけModal等のGPU環境へ投げる。

```txt
Local UI
  ↓
Local Project Manager
  ↓
GPU Worker API
  ↓
Headless ComfyUI
  ↓
結果bytes返却
  ↓
Local Storage保存
  ↓
GPU Worker側temp削除
```

重要条件：

- 生成物を外部GPU側に永続保存しない
- 画像はbytes/base64/一時ファイルで返却
- 返却後にtemp削除
- モデル・LoRA・VAEのみVolume等に保持可能
- プロンプトログは最小化

#### 6.3.3 ハイブリッドモード

軽い処理はローカル、重い生成のみ外部GPU。

| 処理 | 実行場所 |
|---|---|
| プロジェクト管理 | ローカル |
| LLMストーリー生成 | ローカルまたは任意API |
| プロンプト生成 | ローカル |
| 画像生成 | ローカルまたは外部GPU |
| 吹き出し/文字入れ | ローカル |
| PDF/Webtoon出力 | ローカル |

---

## 7. 自動完走パイプライン

### 7.1 完全自動フロー

```txt
PROJECT_CREATED
  ↓
INPUT_VALIDATED
  ↓
STORY_PLANNED
  ↓
CHARACTERS_DEFINED
  ↓
CHARACTER_SHEETS_GENERATED
  ↓
PAGES_PLANNED
  ↓
PANELS_PLANNED
  ↓
PROMPTS_GENERATED
  ↓
WORKFLOWS_BUILT
  ↓
PANELS_GENERATING
  ↓
PANELS_QA_CHECKING
  ↓
PANELS_REPAIRING
  ↓
LETTERING
  ↓
PAGE_RENDERING
  ↓
EXPORTING
  ↓
COMPLETED
```

### 7.2 状態定義

| 状態 | 意味 | 次状態 |
|---|---|---|
| PROJECT_CREATED | プロジェクト作成済み | INPUT_VALIDATED |
| INPUT_VALIDATED | 入力値検証済み | STORY_PLANNED |
| STORY_PLANNED | 作品構成生成済み | CHARACTERS_DEFINED |
| CHARACTERS_DEFINED | キャラ定義済み | CHARACTER_SHEETS_GENERATED |
| CHARACTER_SHEETS_GENERATED | キャラ参照画像生成済み | PAGES_PLANNED |
| PAGES_PLANNED | ページ構成済み | PANELS_PLANNED |
| PANELS_PLANNED | コマ構成済み | PROMPTS_GENERATED |
| PROMPTS_GENERATED | 画像プロンプト生成済み | WORKFLOWS_BUILT |
| WORKFLOWS_BUILT | ComfyUIワークフロー構築済み | PANELS_GENERATING |
| PANELS_GENERATING | コマ画像生成中 | PANELS_QA_CHECKING |
| PANELS_QA_CHECKING | 品質判定中 | PANELS_REPAIRING or LETTERING |
| PANELS_REPAIRING | 失敗コマ修復中 | PANELS_QA_CHECKING |
| LETTERING | セリフ・吹き出し配置中 | PAGE_RENDERING |
| PAGE_RENDERING | ページ画像作成中 | EXPORTING |
| EXPORTING | 出力中 | COMPLETED |
| COMPLETED | 完了 | - |

### 7.3 失敗状態

| 失敗状態 | 原因 | 自動復旧 |
|---|---|---|
| FAILED_INPUT_VALIDATION | 入力不足、矛盾 | デフォルト補完、警告表示 |
| FAILED_STORY_PLANNING | LLM出力不正 | 再プロンプト、JSON修復 |
| FAILED_CHARACTER_SHEET | 参照画像生成失敗 | プロンプト簡略化、再生成 |
| FAILED_WORKFLOW_VALIDATION | ノード不足、モデル不足 | 不足情報表示、代替ワークフロー選択 |
| FAILED_PANEL_GENERATION | ComfyUI生成失敗 | 再投入、別seed、別workflow |
| FAILED_PANEL_QA | 品質不足 | 再生成、修復、構図変更 |
| FAILED_LETTERING | 吹き出し配置不能 | 余白追加、外側配置 |
| FAILED_EXPORT | ファイル出力失敗 | パス再作成、権限確認、再出力 |

### 7.4 自動復旧原則

エラー時に即停止しない。  
原則は以下。

1. 同一条件で再実行
2. seed変更
3. prompt修正
4. workflow変更
5. 低難度構図へ変更
6. fallback画像を採用
7. ユーザー確認待ちに移行

---

## 8. 入力仕様

### 8.1 最小入力

```json
{
  "idea": "魔王に敗れた勇者の妹が、兄の剣を受け継いで復讐する",
  "genre": "dark fantasy",
  "page_count": 8,
  "format": ["webtoon", "pdf"],
  "language": "ja"
}
```

### 8.2 推奨入力

```json
{
  "project_name": "dark_fantasy_sample",
  "title": "黒剣の継承者",
  "idea": "魔王に敗れた勇者の妹が、兄の剣を受け継いで復讐する",
  "genre": "dark fantasy",
  "target_reader": "young adult",
  "tone": "dark, emotional, heroic",
  "page_count": 8,
  "format": ["webtoon", "png_pages", "pdf"],
  "language": "ja",
  "reading_direction": "vertical",
  "visual": {
    "base_model": "anima_or_user_selected_model",
    "art_style": "dark anime manga, cinematic lighting",
    "color_mode": "full_color",
    "line_style": "clean_lineart"
  },
  "generation": {
    "mode": "local",
    "candidate_count": 4,
    "max_retry_per_panel": 5,
    "quality_threshold": 0.78,
    "seed_policy": "character_fixed_panel_random"
  },
  "characters": [
    {
      "name": "リリア",
      "role": "protagonist",
      "description": "銀髪、青い瞳、黒い軽鎧、兄の剣を持つ少女",
      "fixed": true
    }
  ],
  "negative": {
    "global": "low quality, bad anatomy, bad hands, extra fingers, text, watermark"
  }
}
```

### 8.3 入力値検証

| 項目 | 必須 | ルール |
|---|---|---|
| idea | 必須 | 10文字以上 |
| genre | 任意 | 未指定なら `fantasy` |
| page_count | 必須 | 1〜64。MVPでは1〜8 |
| format | 必須 | `png_pages`, `webtoon`, `pdf` のいずれか |
| language | 任意 | 未指定なら `ja` |
| visual.base_model | 任意 | 未指定なら既定workflowのモデル |
| generation.candidate_count | 任意 | 1〜8。既定4 |
| generation.max_retry_per_panel | 任意 | 0〜10。既定5 |

### 8.4 入力不足時の補完

| 不足項目 | 補完値 |
|---|---|
| title | LLMで自動生成 |
| genre | `fantasy` |
| page_count | 4 |
| format | `png_pages` |
| style | `anime manga` |
| language | `ja` |
| character | LLMで主人公を作成 |

---

## 9. 出力仕様

### 9.1 出力ディレクトリ

```txt
user_data/manga_autopilot/
  projects/
    {project_id}/
      project.json
      story.json
      characters.json
      pages.json
      panels.json
      bubbles.json
      workflows.json
      generation_log.json
      qa_report.json
      manifest.json
      assets/
        characters/
        panels/
        pages/
        temp/
      exports/
        pages/
          page_001.png
          page_002.png
        webtoon/
          webtoon_001.png
        pdf/
          manga.pdf
```

### 9.2 必須出力ファイル

| ファイル | 内容 |
|---|---|
| project.json | プロジェクト全体設定 |
| story.json | ストーリー構成 |
| characters.json | キャラクター情報 |
| pages.json | ページ構成 |
| panels.json | コマ情報 |
| bubbles.json | 吹き出し・セリフ |
| generation_log.json | 生成履歴 |
| qa_report.json | 品質チェック結果 |
| manifest.json | 出力物一覧 |
| page_XXX.png | 各ページ画像 |
| webtoon_XXX.png | Webtoon縦長画像 |
| manga.pdf | PDF出力 |

### 9.3 manifest.json例

```json
{
  "project_id": "proj_20260607_001",
  "title": "黒剣の継承者",
  "status": "COMPLETED",
  "created_at": "2026-06-07T12:00:00+09:00",
  "completed_at": "2026-06-07T12:48:00+09:00",
  "exports": {
    "pages": [
      "exports/pages/page_001.png",
      "exports/pages/page_002.png"
    ],
    "webtoon": [
      "exports/webtoon/webtoon_001.png"
    ],
    "pdf": "exports/pdf/manga.pdf"
  },
  "stats": {
    "page_count": 8,
    "panel_count": 32,
    "generated_images": 128,
    "regenerated_panels": 9,
    "average_qa_score": 0.82
  }
}
```

---

## 10. ComfyUI連携仕様

### 10.1 ComfyUI Clientの責務

ComfyUI Client は、Manga Autopilot と ComfyUI Server API の間をつなぐ。

責務：

- API形式ワークフロー送信
- WebSocket進捗監視
- 生成履歴取得
- 画像取得
- 参照画像アップロード
- ノード情報取得
- queue状態取得
- interrupt/free等の管理API呼び出し

### 10.2 使用するComfyUI API

| API | Method | 用途 |
|---|---:|---|
| `/prompt` | POST | ワークフロー投入 |
| `/prompt` | GET | queue状態確認 |
| `/history/{prompt_id}` | GET | 指定promptの履歴取得 |
| `/history` | GET | 履歴一覧取得 |
| `/view` | GET | 画像取得 |
| `/upload/image` | POST | 参照画像アップロード |
| `/upload/mask` | POST | マスクアップロード |
| `/object_info` | GET | 全ノード型情報取得 |
| `/object_info/{node_class}` | GET | 特定ノード情報取得 |
| `/ws` | WebSocket | 進捗監視 |
| `/queue` | GET/POST | Queue確認/制御 |
| `/interrupt` | POST | 実行中断 |
| `/system_stats` | GET | VRAM/環境確認 |
| `/models` | GET | モデル種別一覧 |
| `/models/{folder}` | GET | 指定モデル一覧 |

### 10.3 ComfyUI API投入形式

`/prompt` には、UI保存形式ではなく、API形式のワークフローを送る。

```json
{
  "prompt": {
    "3": {
      "class_type": "KSampler",
      "inputs": {
        "seed": 123456789,
        "steps": 28,
        "cfg": 7,
        "sampler_name": "dpmpp_2m",
        "scheduler": "karras"
      }
    }
  },
  "client_id": "manga_autopilot_client"
}
```

### 10.4 生成結果取得フロー

```txt
1. workflowを構築
2. POST /prompt
3. prompt_idを取得
4. /wsで進捗監視
5. execution完了を検知
6. GET /history/{prompt_id}
7. 出力ファイル名を抽出
8. GET /view?filename=... で画像取得
9. ローカルproject assetsへ保存
```

### 10.5 参照画像アップロードフロー

```txt
1. ローカル参照画像を選択
2. POST /upload/image
3. ComfyUI側input/temp/outputの保存先を取得
4. workflow内のLoadImageノードへfilenameを設定
5. /promptへ投入
```

### 10.6 WebSocket進捗イベント

監視対象：

- `status`
- `execution_start`
- `execution_cached`
- `executing`
- `progress`
- `executed`
- `execution_error`

UI表示：

| イベント | UI表示 |
|---|---|
| execution_start | ジョブ開始 |
| executing | 現在実行ノード表示 |
| progress | プログレスバー更新 |
| executed | ノード完了 |
| execution_error | エラー表示・Retry Controllerへ通知 |

---

## 11. ComfyUI拡張方式

### 11.1 拡張の基本方針

Manga Autopilot は、ComfyUIのcustom node/extensionとして配置する。

```txt
ComfyUI/
  custom_nodes/
    ComfyUI-Manga-Autopilot/
      __init__.py
      pyproject.toml
      src/
      web/
      workflows/
```

### 11.2 `WEB_DIRECTORY`

ComfyUIのJavaScript拡張としてUIを読み込む。

`__init__.py` 例：

```python
WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
```

### 11.3 JavaScript拡張登録

```js
import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "comfyui.manga.autopilot",
  async setup() {
    // Manga Autopilotのタブ、サイドバー、メニューを登録する
  }
});
```

### 11.4 UIの配置場所

候補：

| 配置 | 説明 | 優先度 |
|---|---|---:|
| Sidebar Tab | ComfyUIのサイドバーに表示 | 高 |
| Bottom Panel | 生成進捗やジョブキュー表示に向く | 中 |
| Full Page Route | 漫画エディタに向く | 高 |
| Topbar Menu | 起動導線として利用 | 中 |

MVPでは、**Sidebar Tab + Full Page風の独自画面**とする。

### 11.5 API用カスタムルート

ComfyUI拡張内で独自APIを提供する。

例：

```python
from aiohttp import web
from server import PromptServer

routes = PromptServer.instance.routes

@routes.get('/manga_autopilot/projects')
async def list_projects(request):
    return web.json_response({"projects": []})

@routes.post('/manga_autopilot/projects')
async def create_project(request):
    data = await request.json()
    return web.json_response({"ok": True})
```

独自APIは `/manga_autopilot/*` に統一する。

---

## 12. ワークフロー管理仕様

### 12.1 Workflow Registry

ユーザーはComfyUI API形式ワークフローを登録できる。

対応workflow種別：

| 種別 | 用途 |
|---|---|
| text_to_image | 通常コマ生成 |
| image_to_image | 前コマ参照、修正生成 |
| reference_to_image | キャラ参照画像付き生成 |
| character_sheet | キャラシート生成 |
| face_detail | 顔修正 |
| inpaint | 部分修正 |
| upscale | 高解像度化 |
| background_only | 背景だけ生成 |
| pose_control | ControlNet/OpenPose等 |
| lineart_control | ラフ/線画制御 |

### 12.2 Workflow Binding

Manga Autopilotの内部項目をComfyUIノードへ対応付ける。

```json
{
  "workflow_id": "anime_t2i_default",
  "name": "Anime T2I Default",
  "type": "text_to_image",
  "file": "workflows/anime_t2i_api.json",
  "bindings": {
    "positive_prompt": { "node_id": "6", "input": "text" },
    "negative_prompt": { "node_id": "7", "input": "text" },
    "seed": { "node_id": "3", "input": "seed" },
    "steps": { "node_id": "3", "input": "steps" },
    "cfg": { "node_id": "3", "input": "cfg" },
    "width": { "node_id": "5", "input": "width" },
    "height": { "node_id": "5", "input": "height" },
    "checkpoint": { "node_id": "4", "input": "ckpt_name" },
    "filename_prefix": { "node_id": "9", "input": "filename_prefix" }
  }
}
```

### 12.3 必須バインド

text_to_image workflowでは以下が必須。

- positive_prompt
- negative_prompt
- seed
- width
- height
- output_node or filename_prefix

reference_to_image workflowでは追加で以下が必要。

- reference_image
- reference_strength or ip_adapter_strength

### 12.4 Workflow Validator

登録時・実行前に以下を検証する。

| 検証項目 | 内容 |
|---|---|
| JSON形式 | JSONとして読めるか |
| API形式 | `/prompt`に渡せる形式か |
| node_id存在 | bindingsで指定したnode_idがあるか |
| input存在 | 指定inputがnodeにあるか |
| class_type存在 | class_typeがobject_infoにあるか |
| model存在 | ckpt/LoRA/VAE等が存在するか |
| custom node存在 | 必要なカスタムノードが読み込まれているか |
| output存在 | SaveImage等の出力ノードがあるか |
| security | 外部通信・任意コード実行ノードの警告 |

### 12.5 Workflow Binder UI

画面要素：

- workflow JSONアップロード
- workflow type選択
- ノード一覧表示
- ノードごとのinput一覧表示
- Manga項目との対応付け
- 検証ボタン
- テスト生成ボタン
- 保存ボタン

---

## 13. キャラクター管理仕様

### 13.1 Characterモデル

```ts
type Character = {
  id: string;
  name: string;
  role: "protagonist" | "heroine" | "villain" | "support" | "mob";
  description: string;
  personality: string;
  ageAppearance?: string;
  appearance: CharacterAppearance;
  outfit: Outfit;
  colorPalette: ColorPalette;
  referenceImages: AssetRef[];
  expressionImages: AssetRef[];
  lora?: LoraRef;
  ipAdapterRef?: AssetRef;
  consistencyPrompt: string;
  negativePrompt: string;
  fixed: boolean;
};
```

### 13.2 CharacterAppearance

```ts
type CharacterAppearance = {
  genderExpression?: string;
  hairColor: string;
  hairStyle: string;
  eyeColor: string;
  faceFeatures: string[];
  bodyType?: string;
  height?: string;
  distinctiveFeatures: string[];
};
```

### 13.3 Outfit

```ts
type Outfit = {
  base: string;
  upper?: string;
  lower?: string;
  shoes?: string;
  accessories: string[];
  weapon?: string;
  mustKeep: string[];
  mustAvoid: string[];
};
```

### 13.4 キャラ固定レベル

| Level | 方法 | MVP | v1.0 |
|---|---|---:|---:|
| 1 | プロンプト固定 | 必須 | 必須 |
| 2 | キャラシート生成 | 任意 | 必須 |
| 3 | 参照画像利用 | 任意 | 必須 |
| 4 | IP-Adapter | 任意 | 推奨 |
| 5 | LoRA | 手動 | 任意対応 |
| 6 | Face Detailer | 任意 | 推奨 |
| 7 | 顔埋め込み/類似度チェック | 後回し | 任意 |

### 13.5 キャラシート生成

キャラ定義後、以下の画像を生成する。

```txt
front view
side view
back view
face close-up
expression sheet
outfit detail sheet
```

保存先：

```txt
assets/characters/{character_id}/
  reference_front.png
  reference_side.png
  reference_back.png
  reference_face.png
  expression_sheet.png
  outfit_sheet.png
  character_card.json
```

### 13.6 キャラプロンプト生成ルール

キャラの重要特徴はプロンプト先頭に置く。

例：

```txt
1girl, silver long hair, blue eyes, black light armor, small scar under left eye, holding black sword,
```

キャラの固定要素は `mustKeep` として管理する。

```json
{
  "mustKeep": [
    "silver long hair",
    "blue eyes",
    "black light armor",
    "black sword",
    "small scar under left eye"
  ],
  "mustAvoid": [
    "short hair",
    "red hair",
    "casual clothes",
    "different weapon"
  ]
}
```

### 13.7 表情プリセット

```json
[
  "neutral",
  "smile",
  "angry",
  "sad",
  "crying",
  "surprised",
  "determined",
  "embarrassed",
  "fear",
  "pain",
  "shouting",
  "relieved",
  "confused",
  "serious",
  "despair"
]
```

### 13.8 ポーズプリセット

```json
[
  "standing",
  "running",
  "walking",
  "falling",
  "kneeling",
  "looking back",
  "holding sword",
  "battle stance",
  "reaching hand",
  "turning around",
  "close-up face",
  "upper body shot",
  "from behind",
  "low angle",
  "high angle"
]
```

---

## 14. ストーリー生成仕様

### 14.1 Story Plannerの責務

入力された企画を、漫画制作に使える構造化JSONへ分解する。

生成項目：

- title
- logline
- theme
- genre
- mood
- characters
- acts
- pages
- panels

### 14.2 StoryPlanモデル

```ts
type StoryPlan = {
  title: string;
  logline: string;
  theme: string;
  genre: string;
  mood: string;
  acts: Act[];
  pages: PagePlan[];
};
```

### 14.3 Actモデル

```ts
type Act = {
  id: string;
  name: string;
  startPage: number;
  endPage: number;
  summary: string;
  emotionalArc: string;
};
```

### 14.4 PagePlanモデル

```ts
type PagePlan = {
  pageNumber: number;
  summary: string;
  emotionalGoal: string;
  visualGoal: string;
  panelCount: number;
  cliffhanger?: string;
};
```

### 14.5 PanelPlanモデル

```ts
type PanelPlan = {
  panelNumber: number;
  purpose: string;
  shot: string;
  cameraAngle: string;
  characters: string[];
  background: string;
  action: string;
  emotion: string;
  dialogue: Dialogue[];
  sfx: SoundEffect[];
  visualPriority: "character" | "action" | "background" | "emotion";
};
```

### 14.6 LLMプロンプト：Story Planner

```txt
あなたは漫画原作者です。
以下の企画を、指定ページ数の漫画構成にしてください。

条件:
- 出力はJSONのみ
- ページ数: {{page_count}}
- 言語: {{language}}
- ジャンル: {{genre}}
- 1ページごとに summary, emotionalGoal, visualGoal, panelCount を含める
- セリフは短くする
- 各ページの目的が重複しないようにする
- 最終ページには読後感または次への引きを入れる

企画:
{{idea}}
```

### 14.7 LLM出力の検証

| 検証 | ルール |
|---|---|
| JSON | パース可能であること |
| page数 | 指定page_countと一致 |
| panel数 | 各ページ1〜8コマ |
| dialogue | 1吹き出し40文字以内推奨 |
| characters | 存在するキャラIDと一致 |
| visualGoal | 空でない |

不正な場合はJSON修復プロンプトを実行する。

---

## 15. コマ割り仕様

### 15.1 レイアウト方針

MVPではテンプレート方式。  
v1.0ではテンプレート + 自動選択。

### 15.2 ページ漫画テンプレート

| ID | 名称 | 用途 |
|---|---|---|
| page_1_full | 1ページ1大ゴマ | 決めシーン、扉絵 |
| page_2_vertical | 2コマ縦 | 対比、会話 |
| page_2_horizontal | 2コマ横 | 横移動、比較 |
| page_3_standard | 3コマ標準 | 汎用 |
| page_4_grid | 4コマグリッド | 会話、説明 |
| page_5_dynamic | 5コマ変則 | アクション |
| page_climax | クライマックス大ゴマ | 見せ場 |

### 15.3 Webtoonテンプレート

| ID | 名称 | 用途 |
|---|---|---|
| wt_single | 1カラム単発 | 汎用 |
| wt_dialogue | 会話テンポ型 | キャラ会話 |
| wt_action | アクション型 | 動きのある場面 |
| wt_emotion | 感情アップ型 | 表情重視 |
| wt_reveal | 溜め・開示型 | 重要情報開示 |

### 15.4 自動選択ルール

| シーン | 推奨レイアウト |
|---|---|
| 舞台説明 | ワイド大ゴマ |
| 会話 | 小〜中コマ連続 |
| 感情表現 | 顔アップ大きめ |
| アクション | 大ゴマ + 斜め構図 |
| 決め台詞 | 余白多め + 中央配置 |
| 回想 | 枠線薄め、色調変化 |
| 緊張 | コマ間余白を広げる |

### 15.5 PanelLayoutモデル

```ts
type PanelLayout = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  zIndex: number;
  border: {
    width: number;
    color: string;
    radius: number;
  };
  margin: number;
  bleed: boolean;
  rotation?: number;
};
```

---

## 16. プロンプト生成仕様

### 16.1 Prompt Builderの責務

PanelPlanから、画像生成モデル向けプロンプトを生成する。

入力：

- キャラ特徴
- 表情
- アクション
- 背景
- カメラ
- 画風
- 品質指定
- 参照画像設定

出力：

- positive prompt
- negative prompt
- width/height
- seed
- workflow selection
- reference settings

### 16.2 PromptSpecモデル

```ts
type PromptSpec = {
  positive: string;
  negative: string;
  characterPrompt: string;
  backgroundPrompt: string;
  actionPrompt: string;
  cameraPrompt: string;
  emotionPrompt: string;
  stylePrompt: string;
  qualityPrompt: string;
  seed: number;
  width: number;
  height: number;
  steps: number;
  cfg: number;
  sampler: string;
  scheduler: string;
};
```

### 16.3 Positive Prompt構成順

```txt
[キャラ固定要素],
[人数],
[表情],
[ポーズ/アクション],
[服装/小物],
[背景],
[カメラ/構図],
[ライティング],
[漫画表現],
[品質指定]
```

### 16.4 Negative Prompt構成

グローバル：

```txt
low quality, worst quality, blurry, bad anatomy, bad hands, extra fingers, missing fingers, deformed face, text, watermark, logo, cropped, duplicate character
```

キャラ固定：

```txt
different hair color, different eye color, different outfit, wrong weapon, inconsistent costume, wrong age, different character
```

漫画向け：

```txt
speech text in image, unreadable letters, random letters, broken panel border, excessive background clutter
```

### 16.5 LLMプロンプト：Image Prompt Builder

```txt
あなたはStable Diffusion/ComfyUI向けの画像生成プロンプトエンジニアです。
次の漫画コマ情報を英語の画像生成プロンプトに変換してください。

条件:
- 出力はJSONのみ
- positive と negative を分ける
- セリフ、擬音、文字は画像に入れない
- キャラクター固定要素を先頭に置く
- 構図、表情、背景、光、漫画的演出を含める
- 1つのコマに情報を詰め込みすぎない

コマ情報:
{{panel_plan}}

キャラクター情報:
{{characters}}
```

---

## 17. 画像生成ジョブ仕様

### 17.1 GenerationJobモデル

```ts
type GenerationJob = {
  id: string;
  projectId: string;
  pageId: string;
  panelId: string;
  workflowId: string;
  status: JobStatus;
  input: PromptSpec;
  candidates: CandidateImage[];
  selectedCandidateId?: string;
  retryCount: number;
  error?: string;
  promptId?: string;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
};
```

### 17.2 JobStatus

```ts
type JobStatus =
  | "PENDING"
  | "VALIDATING"
  | "QUEUED"
  | "RUNNING"
  | "FETCHING_RESULT"
  | "QA_CHECKING"
  | "RETRYING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";
```

### 17.3 候補生成

1コマにつき複数候補を生成する。

```json
{
  "panel_id": "page_001_panel_001",
  "candidate_count": 4,
  "seed_policy": "base_seed_plus_index",
  "retry_policy": {
    "max_retry": 5,
    "on_fail": "revise_prompt"
  }
}
```

### 17.4 Seed Policy

| policy | 説明 |
|---|---|
| fixed | 全生成で固定seed |
| panel_random | コマごとにランダム |
| character_fixed_panel_random | キャラ系seedは固定、コマは変える |
| base_seed_plus_index | base_seed + candidate index |

推奨：`character_fixed_panel_random`

### 17.5 ジョブ実行疑似コード

```python
def generate_panel_until_pass(panel):
    for attempt in range(panel.max_retry):
        candidates = generate_candidates(panel)
        scored = []

        for candidate in candidates:
            qa = quality_checker.check(candidate, panel)
            scored.append((candidate, qa))

        best_candidate, best_qa = select_best(scored)

        if best_qa.score >= panel.quality_threshold:
            panel.selected_image = best_candidate.asset_ref
            panel.qa = best_qa
            return panel

        panel = retry_controller.revise(panel, best_qa.issues)

    fallback = generate_fallback(panel)
    panel.selected_image = fallback.asset_ref
    panel.qa = fallback.qa
    return panel
```

---

## 18. 品質チェック仕様

### 18.1 QAの目的

完全自動完走では、生成画像の失敗を人間が毎回確認できない。  
そのため、最低限の自動品質チェックを行う。

### 18.2 QA項目

| 項目 | 内容 | MVP | v1.0 |
|---|---|---:|---:|
| prompt_alignment | 指示と画像が合っているか | 任意 | 必須 |
| face_quality | 顔が崩れていないか | 任意 | 必須 |
| hand_quality | 手が大きく崩れていないか | 任意 | 推奨 |
| character_consistency | 髪色・服装などが一致するか | 任意 | 必須 |
| character_count | 人数が合っているか | 任意 | 推奨 |
| bubble_space | 吹き出し余白があるか | 必須 | 必須 |
| text_artifact | 画像内に不要な文字がないか | 任意 | 推奨 |
| sharpness | ぼやけていないか | 任意 | 推奨 |
| composition | 構図が破綻していないか | 任意 | 推奨 |

### 18.3 QualityResultモデル

```ts
type QualityResult = {
  panelId: string;
  candidateId: string;
  score: number;
  passed: boolean;
  checks: {
    promptAlignment: number;
    faceQuality: number;
    handQuality: number;
    characterConsistency: number;
    bubbleSpace: number;
    sharpness: number;
  };
  issues: QualityIssue[];
  suggestedActions: RetryAction[];
};
```

### 18.4 スコア計算

```txt
総合スコア =
  キャラ一致度 30%
  プロンプト一致度 20%
  顔品質 15%
  手品質 10%
  構図 10%
  解像感/ノイズ 10%
  吹き出し余白 5%
```

### 18.5 自動修復ルール

| 問題 | 修復アクション |
|---|---|
| 顔崩れ | face_detail workflow実行 |
| 手崩れ | 構図をupper body/face close-upに変更 |
| キャラ違い | 参照画像強度を上げる、キャラpromptを先頭へ |
| 背景違い | 背景promptを簡略化し強調 |
| 人数違い | `solo`, `1girl`, `two characters` 等を強化 |
| 余白不足 | Webtoon余白追加、吹き出し外側配置 |
| 画像内文字 | negativeにtext/watermarkを追加 |
| 複雑すぎる | promptを簡略化 |

---

## 19. 吹き出し・セリフ仕様

### 19.1 基本方針

**セリフは画像生成に含めない。**

理由：

- 日本語文字が崩れやすい
- 後から修正できない
- 翻訳対応しにくい
- 画像品質評価が難しくなる

### 19.2 SpeechBubbleモデル

```ts
type SpeechBubble = {
  id: string;
  panelId: string;
  type: "normal" | "shout" | "thought" | "narration" | "whisper" | "radio";
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  tailTarget?: { x: number; y: number };
  font: FontSpec;
  direction: "vertical" | "horizontal";
  order: number;
};
```

### 19.3 FontSpec

```ts
type FontSpec = {
  family: string;
  size: number;
  weight: "normal" | "bold";
  lineHeight: number;
  letterSpacing: number;
  color: string;
};
```

### 19.4 吹き出し種別

| type | 用途 | 表現 |
|---|---|---|
| normal | 通常会話 | 丸型/楕円 |
| shout | 叫び | ギザギザ |
| thought | 心の声 | 雲型 |
| narration | ナレーション | 四角枠 |
| whisper | 小声 | 点線/小さめ |
| radio | 通信/機械音 | 角丸四角/ノイズ枠 |

### 19.5 自動配置ルール

1. 顔の上に置かない
2. 重要な手・武器の上に置かない
3. 画面端に寄せすぎない
4. 読む順番に沿う
5. 日本語は縦書き優先
6. Webtoonは横書きも許可
7. 置けない場合はコマ外余白を追加
8. 吹き出し同士を重ねない

### 19.6 セリフ制限

| 項目 | 推奨 |
|---|---:|
| 1吹き出し文字数 | 5〜40文字 |
| 1コマ吹き出し数 | 0〜3個 |
| 1ページ吹き出し数 | 2〜12個 |
| Webtoon 1画面内文字量 | 少なめ |

---

## 20. レンダリング仕様

### 20.1 Page Renderer

処理順：

1. キャンバス作成
2. 背景色設定
3. コマ画像配置
4. コマ枠描画
5. 吹き出し描画
6. セリフ描画
7. 擬音描画
8. ページ番号・余白処理
9. PNG出力

### 20.2 Webtoon Renderer

処理：

1. ページ/コマを縦方向に並べる
2. コマ間余白を調整
3. シーン転換では大きめ余白
4. 決めシーン前後に間を作る
5. スマホ幅向けにリサイズ
6. 高さが長すぎる場合は分割

推奨サイズ：

```txt
width: 1080px
max_height_per_slice: 12000px
```

### 20.3 PDF Renderer

対応サイズ：

- A4
- B5
- Kindle向け
- カスタム

出力設定：

```json
{
  "pdf_size": "A4",
  "margin_mm": 10,
  "dpi": 300,
  "include_cover": false,
  "reading_direction": "right_to_left"
}
```

---

## 21. バックエンドAPI仕様

### 21.1 API prefix

すべて以下に統一する。

```txt
/manga_autopilot/api
```

### 21.2 Project API

```http
GET    /manga_autopilot/api/projects
POST   /manga_autopilot/api/projects
GET    /manga_autopilot/api/projects/{project_id}
PATCH  /manga_autopilot/api/projects/{project_id}
DELETE /manga_autopilot/api/projects/{project_id}
```

### 21.3 Autopilot API

```http
POST /manga_autopilot/api/projects/{project_id}/autopilot/start
POST /manga_autopilot/api/projects/{project_id}/autopilot/pause
POST /manga_autopilot/api/projects/{project_id}/autopilot/resume
POST /manga_autopilot/api/projects/{project_id}/autopilot/cancel
GET  /manga_autopilot/api/projects/{project_id}/autopilot/status
```

### 21.4 Character API

```http
GET   /manga_autopilot/api/projects/{project_id}/characters
POST  /manga_autopilot/api/projects/{project_id}/characters
GET   /manga_autopilot/api/projects/{project_id}/characters/{character_id}
PATCH /manga_autopilot/api/projects/{project_id}/characters/{character_id}
POST  /manga_autopilot/api/projects/{project_id}/characters/{character_id}/generate-sheet
POST  /manga_autopilot/api/projects/{project_id}/characters/{character_id}/upload-reference
```

### 21.5 Workflow API

```http
GET   /manga_autopilot/api/workflows
POST  /manga_autopilot/api/workflows
GET   /manga_autopilot/api/workflows/{workflow_id}
PATCH /manga_autopilot/api/workflows/{workflow_id}
POST  /manga_autopilot/api/workflows/{workflow_id}/validate
POST  /manga_autopilot/api/workflows/{workflow_id}/test-run
```

### 21.6 Panel API

```http
POST /manga_autopilot/api/projects/{project_id}/pages/{page_id}/panels/{panel_id}/generate
POST /manga_autopilot/api/projects/{project_id}/pages/{page_id}/panels/{panel_id}/regenerate
POST /manga_autopilot/api/projects/{project_id}/pages/{page_id}/panels/{panel_id}/repair
PATCH /manga_autopilot/api/projects/{project_id}/pages/{page_id}/panels/{panel_id}
```

### 21.7 Export API

```http
POST /manga_autopilot/api/projects/{project_id}/export/png
POST /manga_autopilot/api/projects/{project_id}/export/webtoon
POST /manga_autopilot/api/projects/{project_id}/export/pdf
GET  /manga_autopilot/api/projects/{project_id}/exports
```

---

## 22. フロントエンド画面仕様

### 22.1 画面一覧

| 画面 | 用途 | MVP |
|---|---|---:|
| Dashboard | プロジェクト一覧 | 必須 |
| Project Create | 新規作成 | 必須 |
| Autopilot Wizard | 自動生成設定 | 必須 |
| Generation Monitor | 進捗表示 | 必須 |
| Story Editor | ストーリー編集 | v1.0 |
| Character Manager | キャラ管理 | v1.0 |
| Workflow Binder | ワークフロー登録 | MVP |
| Page Editor | 漫画ページ編集 | 必須 |
| Webtoon Preview | 縦長プレビュー | v1.0 |
| Export Center | 出力管理 | 必須 |
| Settings | 全体設定 | 必須 |

### 22.2 Dashboard

表示項目：

- プロジェクト名
- タイトル
- 作成日時
- 最終更新日時
- 状態
- ページ数
- 出力状況

操作：

- 新規作成
- 開く
- 複製
- 削除
- エクスポート

### 22.3 Autopilot Wizard

入力項目：

- 企画文
- ジャンル
- ページ数
- 出力形式
- 絵柄
- モデル/ワークフロー
- キャラ人数
- 自動リトライ回数
- 品質優先/速度優先
- ローカル/外部GPU

実行ボタン：

- `自動生成開始`
- `途中確認しながら開始`
- `設定保存`

### 22.4 Generation Monitor

表示項目：

- 現在ステータス
- 完了ステップ
- 生成中ページ
- 生成中コマ
- 失敗コマ数
- 再生成回数
- 平均QAスコア
- Queue残数
- エラーログ

操作：

- 一時停止
- 再開
- キャンセル
- 失敗コマだけ再生成

### 22.5 Page Editor

機能：

- ページ追加/削除
- コマ追加/削除
- コマ画像差し替え
- コマ再生成
- プロンプト編集
- 吹き出し追加/移動/削除
- セリフ編集
- 擬音追加
- PNG再レンダリング

### 22.6 Workflow Binder画面

機能：

- workflow JSON読み込み
- ノード一覧表示
- input一覧表示
- binding設定
- object_info検証
- テスト生成
- 保存

---

## 23. LLM連携仕様

### 23.1 LLM Provider

```ts
type LLMProvider = {
  id: string;
  type: "local" | "ollama" | "openai_compatible" | "manual";
  endpoint?: string;
  model: string;
  apiKeyEnv?: string;
  temperature: number;
  maxTokens: number;
};
```

### 23.2 推奨Provider

| Provider | 用途 |
|---|---|
| local | 完全ローカル志向 |
| ollama | 導入容易 |
| openai_compatible | vLLM, LM Studio, OpenRouter等 |
| manual | LLMなしで手入力 |

### 23.3 LLMの使用箇所

| 箇所 | 必須度 |
|---|---:|
| タイトル生成 | 任意 |
| ストーリー生成 | 必須 |
| キャラ設定生成 | 必須 |
| ページ構成生成 | 必須 |
| コマ構成生成 | 必須 |
| セリフ生成 | 必須 |
| 画像プロンプト生成 | 必須 |
| 失敗原因からの修正案 | 推奨 |

### 23.4 JSON出力強制

LLM出力はすべてJSON Schemaで検証する。  
不正な場合は修復プロンプトを実行する。

```txt
以下のJSONはパースに失敗しました。
指定Schemaに合うように修復し、JSONのみを返してください。
説明文は不要です。
```

---

## 24. 外部GPU Bridge仕様

### 24.1 目的

ローカルPCのVRAM不足や速度不足を補うため、生成処理だけ外部GPUへ委譲する。

### 24.2 基本方針

- ローカルが主
- 外部GPUは生成worker
- 生成画像を外部に永続保存しない
- 返却後temp削除
- モデルはVolume等に置いてよい
- プロンプトログは最小限

### 24.3 Worker API

```http
POST /generate
```

Request：

```json
{
  "job_id": "job_001",
  "workflow": {},
  "assets": [
    {
      "name": "reference.png",
      "content_base64": "..."
    }
  ],
  "settings": {
    "return_type": "base64",
    "delete_temp_after_return": true,
    "timeout_sec": 900
  }
}
```

Response：

```json
{
  "success": true,
  "job_id": "job_001",
  "images": [
    {
      "filename": "panel_001.png",
      "content_base64": "...",
      "width": 1024,
      "height": 1536
    }
  ],
  "logs": [],
  "deleted_temp": true
}
```

### 24.4 cleanup必須処理

```python
def handle_generate(request):
    temp_paths = []
    try:
        result = run_comfyui_workflow(request, temp_paths)
        return result
    finally:
        for path in temp_paths:
            safe_delete(path)
```

### 24.5 外部GPU失敗時

| 失敗 | 処理 |
|---|---|
| timeout | ローカルfallbackまたは再試行 |
| GPU unavailable | queue待ち、またはローカルfallback |
| generation error | workflow validation実行 |
| result missing | retry |
| cleanup failure | 警告ログ、次回起動時に削除 |

---

## 25. 技術選定

### 25.1 フロントエンド

推奨：

- TypeScript
- React
- Vite
- Zustand or Jotai
- Konva.js

理由：

- ComfyUI拡張UIと相性がよい
- Canvas編集がしやすい
- OSS参加者が多い
- 型定義で複雑なデータモデルを扱いやすい

### 25.2 バックエンド

推奨：

- Python
- aiohttp route integration
- Pydantic
- SQLite
- Pillow
- ReportLab or WeasyPrint

理由：

- ComfyUIと同じPython環境
- custom node/extensionとして同梱しやすい
- 画像処理がしやすい

### 25.3 保存

MVP：

- JSON + ローカルファイル

v1.0：

- SQLite + ローカルファイル

将来：

- PostgreSQL
- S3互換ストレージ

### 25.4 テスト

- Python: pytest
- TypeScript: vitest
- E2E: Playwright
- 画像比較: perceptual hash
- JSON Schema: jsonschema / pydantic

---

## 26. フォルダ構成

```txt
ComfyUI-Manga-Autopilot/
  README.md
  LICENSE
  pyproject.toml
  package.json
  tsconfig.json
  vite.config.ts

  __init__.py

  src/
    manga_autopilot/
      __init__.py
      routes/
        project_routes.py
        autopilot_routes.py
        character_routes.py
        workflow_routes.py
        export_routes.py
      services/
        project_manager.py
        story_planner.py
        character_manager.py
        prompt_builder.py
        workflow_registry.py
        workflow_validator.py
        comfy_client.py
        job_queue.py
        quality_checker.py
        retry_controller.py
        lettering_engine.py
        renderer.py
        exporter.py
        modal_bridge.py
        asset_cleaner.py
      models/
        project.py
        story.py
        character.py
        page.py
        panel.py
        prompt.py
        workflow.py
        job.py
        quality.py
        export.py
      storage/
        file_storage.py
        sqlite_storage.py
      utils/
        json_repair.py
        image_utils.py
        path_utils.py
        logging.py

  web/
    index.js
    dist/
    src/
      main.tsx
      App.tsx
      api/
      components/
      pages/
      editor/
      stores/
      types/
      styles/

  workflows/
    anime_t2i_api.json
    anime_i2i_api.json
    character_sheet_api.json
    reference_t2i_api.json
    face_detail_api.json
    upscale_api.json

  schemas/
    project.schema.json
    story.schema.json
    character.schema.json
    workflow_binding.schema.json

  examples/
    sample_project/
    sample_workflows/

  tests/
    backend/
    frontend/
    e2e/

  docs/
    install.md
    quickstart.md
    workflow_binding.md
    modal_bridge.md
    character_consistency.md
    troubleshooting.md
```

---

## 27. 設定ファイル仕様

### 27.1 config.yaml

```yaml
app:
  name: ComfyUI Manga Autopilot
  language: ja
  storage_path: ./user_data/manga_autopilot
  autosave_interval_sec: 10

comfyui:
  base_url: http://127.0.0.1:8188
  timeout_sec: 600
  use_websocket: true
  client_id: manga_autopilot_client

generation:
  default_candidate_count: 4
  max_retry_per_panel: 5
  quality_threshold: 0.78
  default_width: 768
  default_height: 1024
  default_steps: 28
  default_cfg: 7
  default_sampler: dpmpp_2m
  default_scheduler: karras

character:
  use_reference: true
  use_lora: false
  default_reference_strength: 0.65
  generate_character_sheet: true

llm:
  provider: ollama
  endpoint: http://127.0.0.1:11434
  model: qwen2.5:7b-instruct
  temperature: 0.7
  max_tokens: 4096

modal:
  enabled: false
  endpoint: ""
  api_key_env: MODAL_MANGA_AUTOPILOT_KEY
  delete_temp_after_return: true
  return_type: base64
  timeout_sec: 900

export:
  webtoon_width: 1080
  max_webtoon_slice_height: 12000
  pdf_size: A4
  dpi: 300

security:
  allow_remote_comfyui: false
  warn_unknown_custom_nodes: true
  warn_external_network_nodes: true
  mask_prompt_in_logs: false
```

### 27.2 設定優先順位

```txt
Project settings
  > User config.yaml
  > Default config
```

---

## 28. テスト仕様

### 28.1 Unit Test

| 対象 | テスト内容 |
|---|---|
| Project Manager | 作成、保存、読み込み、削除 |
| Story Planner | JSON Schema準拠 |
| Prompt Builder | 入力からprompt生成 |
| Workflow Validator | node/input/model検証 |
| Comfy Client | API request構築 |
| Quality Checker | score計算 |
| Retry Controller | issueごとの修復方針 |
| Renderer | PNG生成 |
| Exporter | PDF/Webtoon生成 |

### 28.2 Integration Test

- ComfyUI接続テスト
- `/object_info` 取得
- workflow validate
- `/prompt` 投入
- `/history/{prompt_id}` 取得
- `/view` 画像取得
- プロジェクト保存〜再読み込み
- 1ページ生成完走

### 28.3 E2E Test

MVP E2E：

```txt
1. 新規プロジェクト作成
2. 4コマテンプレート選択
3. 各コマにprompt/serif入力
4. 画像生成
5. 吹き出し配置
6. PNG出力
7. ファイル存在確認
```

v1.0 E2E：

```txt
1. 企画文入力
2. Autopilot開始
3. Story/Character/Page/Panel自動生成
4. 全コマ生成
5. QAと再生成
6. Webtoon/PDF出力
7. manifest確認
```

### 28.4 受け入れ条件

| 機能 | 受け入れ条件 |
|---|---|
| プロジェクト作成 | project.jsonが生成される |
| workflow登録 | validationを通過する |
| コマ生成 | 画像がassets/panelsに保存される |
| 吹き出し配置 | PNGに文字が描画される |
| Webtoon出力 | 縦長画像が生成される |
| PDF出力 | PDFが開ける |
| Autopilot | COMPLETED状態まで到達する |

---

## 29. セキュリティ仕様

### 29.1 ローカル運用の原則

- ComfyUIは原則 `127.0.0.1` バインド
- LAN公開時は警告
- 外部公開時はリバースプロキシ・認証必須

### 29.2 ワークフロー安全性

登録workflowに対して以下を警告する。

- 任意コード実行系ノード
- 外部通信系ノード
- ファイル削除・移動系ノード
- 未知のcustom node
- モデルパス外参照

### 29.3 外部GPU利用時

- APIキー必須
- HTTPS推奨
- 一時ファイル削除
- promptログ最小化
- 画像の永続保存禁止
- temp cleanupのfinally実行

### 29.4 ログ方針

ログに残す：

- job_id
- status
- error type
- duration
- retry count

必要に応じて隠す：

- prompt全文
- キャラ設定全文
- 入力画像パス
- 外部APIキー

---

## 30. 実装フェーズ

### Phase 0: 土台

目的：ComfyUI拡張として動く最小構成。

タスク：

- リポジトリ作成
- ComfyUI custom_nodes配置
- `WEB_DIRECTORY`設定
- React/Vite build導線
- 独自API route追加
- config読み込み
- project storage実装

完了条件：

- ComfyUI内にManga Autopilot UIが表示される
- `/manga_autopilot/api/health` が返る

### Phase 1: 手動漫画生成MVP

目的：人間入力で漫画ページを生成できる。

タスク：

- プロジェクト作成
- ページ/コマモデル
- コマ割りテンプレート
- prompt入力
- ComfyUI `/prompt`実行
- `/history`から画像取得
- コマ配置
- 吹き出し追加
- PNG出力

完了条件：

- 4コマ漫画をPNG出力できる

### Phase 2: ワークフロー登録

目的：ユーザーの既存ComfyUIワークフローを使える。

タスク：

- workflow JSON登録
- binding UI
- validator
- object_info連携
- test-run

完了条件：

- 任意のt2i API workflowを登録してコマ生成できる

### Phase 3: Story Autopilot

目的：企画文からストーリー/ページ/コマ構成を作る。

タスク：

- LLM Provider
- Story Planner
- Character Planner
- Page Planner
- Panel Planner
- JSON Schema検証

完了条件：

- 企画文からpages/panels JSONが生成される

### Phase 4: キャラ固定

目的：同一キャラを継続利用できる。

タスク：

- Character Manager
- キャラシート生成
- 参照画像登録
- reference workflow対応
- LoRA設定
- 表情プリセット

完了条件：

- 同一キャラ指定で複数コマ生成できる

### Phase 5: QA/Retry

目的：失敗コマを自動修復する。

タスク：

- 複数候補生成
- QA score
- issue classification
- prompt revision
- retry controller
- fallback

完了条件：

- 低品質候補を避けて採用できる
- 失敗コマのみ再生成できる

### Phase 6: 完全自動完走

目的：企画入力から出力まで自動化。

タスク：

- Autopilot state machine
- pause/resume/cancel
- progress monitor
- error recovery
- Webtoon/PDF export
- manifest出力

完了条件：

- 4〜8ページ作品が自動完走する

### Phase 7: 外部GPU Bridge

目的：重い生成をModal等へ委譲。

タスク：

- Worker API
- asset送信
- workflow送信
- base64返却
- temp cleanup
- fallback

完了条件：

- 外部GPUで生成し、結果だけローカル保存できる

---

## 31. GitHub Issue分割

### Epic 1: プロジェクト基盤

- #1 リポジトリ初期化
- #2 ComfyUI custom node構成作成
- #3 `WEB_DIRECTORY` によるUI読み込み
- #4 health API追加
- #5 config.yaml読み込み
- #6 ローカルstorage path作成
- #7 project.json保存/読み込み

### Epic 2: ComfyUI API連携

- #10 ComfyUI Client実装
- #11 `/prompt` 実行
- #12 `/ws` 進捗監視
- #13 `/history/{prompt_id}` 取得
- #14 `/view` 画像保存
- #15 `/upload/image` 実装
- #16 `/object_info` 取得
- #17 system_stats表示

### Epic 3: Workflow Registry

- #20 workflow登録API
- #21 workflow一覧UI
- #22 binding model作成
- #23 binding UI
- #24 workflow validator
- #25 test-run
- #26 sample workflow同梱

### Epic 4: 漫画エディタ

- #30 Page model
- #31 Panel model
- #32 layout templates
- #33 Konva canvas editor
- #34 panel image placement
- #35 panel border drawing
- #36 project autosave
- #37 PNG renderer

### Epic 5: 吹き出し/セリフ

- #40 SpeechBubble model
- #41 normal bubble drawing
- #42 shout bubble drawing
- #43 thought bubble drawing
- #44 vertical Japanese text
- #45 auto bubble placement
- #46 dialogue editor

### Epic 6: Story Autopilot

- #50 LLM Provider interface
- #51 JSON schema validation
- #52 Story Planner
- #53 Character Planner
- #54 Page Planner
- #55 Panel Planner
- #56 Prompt Builder
- #57 JSON repair

### Epic 7: Character Consistency

- #60 Character Manager UI
- #61 Character data model
- #62 Character sheet workflow
- #63 Reference image upload
- #64 IP-Adapter binding support
- #65 LoRA setting support
- #66 expression preset
- #67 character prompt locking

### Epic 8: QA and Retry

- #70 Candidate generation
- #71 QA score model
- #72 bubble space checker
- #73 prompt alignment checker
- #74 character consistency checker
- #75 retry controller
- #76 prompt revision rules
- #77 fallback generation

### Epic 9: Autopilot

- #80 State machine
- #81 Autopilot start API
- #82 pause/resume/cancel
- #83 progress monitor UI
- #84 error recovery
- #85 completion report
- #86 manifest output

### Epic 10: Export

- #90 page PNG export
- #91 webtoon renderer
- #92 webtoon slicing
- #93 PDF export
- #94 project export/import
- #95 export center UI

### Epic 11: External GPU Bridge

- #100 worker API spec
- #101 Modal worker prototype
- #102 workflow serialization
- #103 asset upload/base64
- #104 result return/base64
- #105 temp cleanup
- #106 local fallback

### Epic 12: Documentation/OSS

- #120 README
- #121 install guide
- #122 quickstart
- #123 workflow binding guide
- #124 character consistency guide
- #125 troubleshooting
- #126 contribution guide
- #127 issue templates

---

## 32. AIエージェント向け実装ルール

AIエージェントがこのプロジェクトのIssueを実装する場合、以下を守る。

### 32.1 ブランチ運用

- Issueごとにブランチを作成する
- `feature/issue-番号-短い説明` を基本とする
- 例：`feature/issue-10-comfy-client`
- 作業完了後はPRを作成する前提で進める

### 32.2 実装前確認

各Issueで必ず確認すること：

1. 関連モデル
2. 関連API
3. 関連UI
4. 保存形式
5. テスト要件
6. 既存コードへの影響

### 32.3 完了条件

各Issueは以下を満たすまで完了にしない。

- 実装完了
- unit test追加
- 必要ならintegration test追加
- 型エラーなし
- lint通過
- READMEまたは該当doc更新
- 受け入れ条件を満たす

### 32.4 禁止事項

- Issue外の大規模リファクタ
- 既存APIの破壊的変更
- workflow JSONの無断変更
- 秘密情報のコミット
- 外部APIキーの直書き
- ユーザー画像の外部永続保存

---

## 33. ライセンス方針

### 33.1 本体ライセンス候補

| ライセンス | 向き |
|---|---|
| MIT | 普及優先 |
| Apache-2.0 | 商用利用許容 + 特許条項 |
| GPL-3.0 | ComfyUI周辺との親和性 |
| AGPL-3.0 | SaaSクローズド利用を防ぎたい場合 |

推奨：

- 普及優先なら **MIT or Apache-2.0**
- SaaSクローズド利用を防ぎたいなら **AGPL-3.0**

### 33.2 モデル・LoRAライセンス

本体ライセンスと、ユーザーが使うモデル/LoRAのライセンスは別。  
UI上で以下を明示する。

```txt
生成に使用するモデル、LoRA、VAE、ControlNet等のライセンスは各配布元に従ってください。
```

### 33.3 生成物の扱い

生成物の利用可否は、使用モデル・LoRA・素材・参照画像の規約に依存する。  
本ソフトは利用権を保証しない。

---

## 34. ドキュメント構成

必須ドキュメント：

```txt
docs/
  install.md
  quickstart.md
  workflow_binding.md
  character_consistency.md
  autopilot.md
  modal_bridge.md
  troubleshooting.md
  development.md
  contribution.md
```

### 34.1 READMEに書く内容

- 何を作るOSSか
- スクリーンショット
- インストール方法
- Quick Start
- 必要環境
- 対応ComfyUI
- 対応workflow
- ライセンス注意
- Contribution

### 34.2 quickstart.md

最短手順：

1. ComfyUIを起動
2. custom_nodesに配置
3. 依存関係インストール
4. UIタブを開く
5. sample workflowを登録
6. sample projectを生成
7. PNG出力

---

## 35. リリース計画

### v0.1.0

- ComfyUI内UI表示
- プロジェクト作成
- 手動4コマ生成
- PNG出力

### v0.2.0

- Workflow Binder
- 任意workflow登録
- 画像取得安定化

### v0.3.0

- 吹き出し/縦書き
- Page Editor改善
- project import/export

### v0.4.0

- LLM Story Planner
- Page/Panel自動生成
- Prompt Builder

### v0.5.0

- Character Manager
- Character Sheet
- Reference Image

### v0.6.0

- Candidate generation
- QA scoring
- Auto retry

### v0.7.0

- Autopilot state machine
- 完全自動生成

### v0.8.0

- Webtoon export
- PDF export

### v0.9.0

- Modal Bridge
- 外部GPU対応

### v1.0.0

- 4〜8ページ自動完走
- Docs整備
- テスト整備
- OSS公開安定版

---

## 36. 最終完成定義

v1.0の完成定義は以下。

```txt
ユーザーが短い企画を入力する
  ↓
システムがストーリー・キャラ・ページ・コマを自動設計する
  ↓
ComfyUIで全コマ画像を生成する
  ↓
品質チェックで失敗コマだけ再生成する
  ↓
吹き出しとセリフを自動配置する
  ↓
Webtoon/PDF/PNGとして出力する
  ↓
project.jsonから再編集できる
```

必須条件：

- ローカルComfyUIで動作する
- UIから一通り操作できる
- 4ページ以上の作品を自動完走できる
- ワークフロー登録が可能
- 生成画像をページに配置できる
- セリフを後乗せできる
- PNG/Webtoon/PDF出力できる
- 失敗時に再開できる
- ドキュメントだけで新規開発者が参加できる

---

## 37. 参考情報

本仕様書は、ComfyUIの公式ドキュメントにおける以下の前提を実装方針に反映している。

- ComfyUIはクライアント/サーバーモデルで、サーバー側Python、クライアント側JavaScriptで構成される。
- ComfyUI Server APIには、`/prompt`, `/history`, `/view`, `/upload/image`, `/object_info`, `/ws` などが存在する。
- `/prompt` はワークフローを検証して実行キューへ投入し、成功時に `prompt_id` と queue number を返す。
- `/ws` は実行開始、実行中ノード、進捗、完了、エラー等のリアルタイム通知に使える。
- JavaScript拡張は `WEB_DIRECTORY` と `app.registerExtension` を使ってComfyUIクライアントに登録できる。
- ComfyUI Workflow JSONはJSON Schemaで定義されている。

参考URL：

- https://docs.comfy.org/custom-nodes/overview
- https://docs.comfy.org/custom-nodes/js/javascript_overview
- https://docs.comfy.org/custom-nodes/walkthrough
- https://docs.comfy.org/development/comfyui-server/comms_routes
- https://docs.comfy.org/development/comfyui-server/api-examples
- https://docs.comfy.org/specs/workflow_json
- https://github.com/comfy-org/comfyui

---

## 38. 付録A: 最小project.json例

```json
{
  "id": "proj_20260607_001",
  "name": "dark_fantasy_sample",
  "title": "黒剣の継承者",
  "idea": "魔王に敗れた勇者の妹が、兄の剣を受け継いで復讐する",
  "language": "ja",
  "status": "PROJECT_CREATED",
  "settings": {
    "page_count": 4,
    "format": ["png_pages"],
    "generation": {
      "candidate_count": 4,
      "max_retry_per_panel": 5,
      "quality_threshold": 0.78
    }
  },
  "created_at": "2026-06-07T12:00:00+09:00",
  "updated_at": "2026-06-07T12:00:00+09:00"
}
```

---

## 39. 付録B: 最小Panel例

```json
{
  "id": "panel_001_001",
  "page_id": "page_001",
  "panel_number": 1,
  "scene_purpose": "舞台提示",
  "characters": [],
  "background": "ruined castle under red sky",
  "camera": {
    "shot": "wide shot",
    "angle": "slightly low angle"
  },
  "emotion": "ominous",
  "action": "smoke rising from the castle",
  "dialogue": [],
  "sfx": [
    { "text": "ゴオオ", "type": "ambient" }
  ],
  "prompt": {
    "positive": "ruined castle under red sky, smoke rising, dark fantasy, cinematic manga panel, dramatic lighting",
    "negative": "low quality, blurry, text, watermark",
    "seed": 123456,
    "width": 768,
    "height": 1024,
    "steps": 28,
    "cfg": 7
  },
  "layout": {
    "x": 40,
    "y": 40,
    "width": 1000,
    "height": 600,
    "zIndex": 1,
    "border": { "width": 4, "color": "#000000", "radius": 0 },
    "margin": 16,
    "bleed": false
  }
}
```

---

## 40. 付録C: Autopilot疑似コード

```python
def run_autopilot(project_id: str):
    project = project_manager.load(project_id)

    state.set(project, "INPUT_VALIDATED")
    validate_and_fill_defaults(project)

    state.set(project, "STORY_PLANNED")
    story = story_planner.plan(project.input)
    project.story = story
    project_manager.save(project)

    state.set(project, "CHARACTERS_DEFINED")
    characters = character_manager.define(story, project.input.characters)
    project.characters = characters
    project_manager.save(project)

    state.set(project, "CHARACTER_SHEETS_GENERATED")
    for character in characters:
        if character.fixed:
            character_manager.generate_sheet(project, character)
    project_manager.save(project)

    state.set(project, "PAGES_PLANNED")
    pages = story_planner.plan_pages(story, project.settings.page_count)
    project.pages = pages
    project_manager.save(project)

    state.set(project, "PANELS_PLANNED")
    panels = story_planner.plan_panels(pages, characters)
    project.panels = panels
    project_manager.save(project)

    state.set(project, "PROMPTS_GENERATED")
    for panel in project.panels:
        panel.prompt = prompt_builder.build(panel, characters, project.settings)
    project_manager.save(project)

    state.set(project, "WORKFLOWS_BUILT")
    workflow_validator.validate_project_workflows(project)

    state.set(project, "PANELS_GENERATING")
    for panel in project.panels:
        job_queue.enqueue_panel_generation(project, panel)
    job_queue.run_until_complete(project)

    state.set(project, "PANELS_QA_CHECKING")
    quality_checker.check_all(project)

    if quality_checker.has_failed_panels(project):
        state.set(project, "PANELS_REPAIRING")
        retry_controller.repair_failed_panels(project)

    state.set(project, "LETTERING")
    lettering_engine.place_all(project)

    state.set(project, "PAGE_RENDERING")
    renderer.render_pages(project)

    state.set(project, "EXPORTING")
    exporter.export_all(project)

    state.set(project, "COMPLETED")
    project_manager.save(project)
    return project
```

---

## 41. 付録D: 実装開始チェックリスト

開発開始時に以下を満たすこと。

- [ ] GitHubリポジトリ作成
- [ ] Issueテンプレート作成
- [ ] Branch運用ルール記載
- [ ] ComfyUIの対象バージョン確認
- [ ] custom_nodesで読み込める最小構成作成
- [ ] `WEB_DIRECTORY` のJS読み込み確認
- [ ] health API確認
- [ ] sample workflow用意
- [ ] sample project用意
- [ ] README下書き作成

---

## 42. 付録E: MVP完了チェックリスト

- [ ] ComfyUI内にUIが出る
- [ ] プロジェクト作成できる
- [ ] 4コマテンプレートを選べる
- [ ] 各コマにpromptを入力できる
- [ ] ComfyUI APIで画像生成できる
- [ ] 生成画像をコマに配置できる
- [ ] 吹き出しを追加できる
- [ ] セリフを入力できる
- [ ] PNG出力できる
- [ ] project.jsonを保存できる
- [ ] project.jsonから復元できる

---

## 43. 付録F: v1.0完了チェックリスト

- [ ] 企画文だけでStoryPlanを生成できる
- [ ] CharacterPlanを生成できる
- [ ] キャラシートを生成できる
- [ ] PagePlanを生成できる
- [ ] PanelPlanを生成できる
- [ ] PromptSpecを生成できる
- [ ] 全コマを自動生成できる
- [ ] 複数候補から自動採用できる
- [ ] 品質NG時に再生成できる
- [ ] 吹き出しを自動配置できる
- [ ] Webtoon出力できる
- [ ] PDF出力できる
- [ ] 途中停止から再開できる
- [ ] 外部GPU Bridgeを使える
- [ ] 主要ドキュメントが揃っている
- [ ] GitHub Issue単位で第三者が作業できる

---

## 44. 最終メッセージ

この仕様書における最重要方針は以下である。

> Manga Autopilot は、画像生成AI単体ではなく、漫画制作工程全体を自動化するためのComfyUI拡張である。

単に「画像を作る」のではなく、

```txt
企画 → ストーリー → キャラ → コマ → 生成 → 評価 → 修復 → レタリング → 出力
```

を1本の再開可能なパイプラインとして実装する。  
これにより、Anifusion風の体験をOSS・ローカル・ComfyUI資産活用型で実現する。
