# ComfyUI-Manga-Autopilot Agent Instructions

## 目的（なぜこれを作るか）

ComfyUI 上で漫画 / Webtoon の制作を端から端まで自動化する ComfyUI 拡張。
ストーリーバイブルからシーン状態を組み立て、レイアウトと連続性に対してパネルを計画し、
Anima 系モデルで生成する。

このプロジェクトで作業する前に、必ず次の順で読むこと。

1. `README.md`（日本語は `README.ja.md`）
2. `HANDOFF.md`
3. `git status --short` と直近コミット

## 正本

| 種別 | 正本の場所 |
|---|---|
| Rule（判断軸・制約） | このファイル |
| Knowledge（設計・使い方） | `README.md` / `docs/`（vault: `E:\11_Obsidian\Claude\03_Project\comfyui-manga-autopilot.md`、project: `comfyui-manga-autopilot`） |
| 生成プロファイル | `src/manga_autopilot/profiles/*.json` |
| ワークフロー | `workflows/` |
| Output（成果物） | GitHub `Kataage/ComfyUI-Manga-Autopilot` |

- 現在の作業状態と次の安全な作業は `HANDOFF.md` を正本とする。
- 会話履歴だけを根拠に作業を開始しない。

## 現在の状態（2026-08-27）

- ブランチ `codex/anima-mvp`（111コミット、fd269e8）。**`origin/main` (c88a542) より15コミット先行しており未push**。
  このリポジトリは 2026-08-27 に `C:\Users\kouda\Documents\Codex\2026-08-26\new-chat\work\` から
  ここへ回収された。回収時点のバンドルは `_scratch/2026-08-27-rescue/manga-autopilot.bundle`。
- `origin` の URL が回収元のローカルパスを指したままになっている可能性がある。
  push する前に `git remote -v` が `https://github.com/Kataage/ComfyUI-Manga-Autopilot.git`
  を指していることを確認すること。

## 絶対に壊さないもの

ComfyUI ライブ環境は別インスタンス。**このリポジトリから直接書き換えない。**

```
C:\Users\kouda\AppData\Local\Comfy-Desktop\Data\Packages\ComfyUI    ← ライブ
  \custom_nodes\ComfyUI-Anima-SceneList / -Resolution / comfyui-anima-enhancer
  \user\default\workflows\
```

`custom_nodes` 配下は手動コピーで配置されており、リポジトリとシンボリックリンクでは
繋がっていない。片方を編集しても他方に反映されないので、変更したら両方を明示的に揃える。

## 検証

```
.venv\Scripts\python.exe -m pytest -q
```

2026-08-27 時点で **1038 passed, 15 skipped**。skip はすべて実 Modal / 実 S3 / 実 ComfyUI を
要する opt-in の E2E（`MANGA_AUTOPILOT_REAL_*` 環境変数で有効化）。

venv は回収時に `pyproject.toml` から再作成した。壊れたら以下で作り直せる。

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## やってはいけないこと

- 未push の15コミットを失う操作（`git reset --hard`、ブランチ削除、force push）を確認なく行わない
- ライブ ComfyUI の `custom_nodes` / `workflows` を確認なく上書きしない
- venv を移動しない（Windows の venv は `Scripts\*.exe` に絶対パスが焼き込まれている）
