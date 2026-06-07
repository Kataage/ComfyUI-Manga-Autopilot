# コントリビューション

> 🌐 **言語 / Language:** [English](contribution.md) | **日本語** (このファイル)

ComfyUI Manga Autopilot への貢献に興味を持っていただき、ありがとうございます!
本プロジェクトは Issue 単位で管理し、PR を squash マージする運用です。

## Issue ファースト

すべての変更は GitHub Issue から始まります。対応したいオープンな
Issue を探すか、課題の内容を説明する新しい Issue を作成してください。

## ブランチ命名

```text
{issue_number}-{type}
```

`{type}` は `feature` / `bug` / `docs` / `refactor` / `chore` / `test` の
いずれかです。例: `48-feature`。

## コミットフォーマット

```text
{type}: <短い要約> #{issue_number}
```

例: `feat: character service + Character Manager UI #47`

## プルリクエスト

- 1 Issue = 1 ブランチ
- チェックが通ったら `--delete-branch --admin` で squash マージ
- タイトルと本文で対応する Issue を `Closes #N` (フォローアップの場合は
  `Refs #N`) で参照する
- `pytest tests/backend/` と `ruff check .` の両方が通っていること

## ローカルチェック

```bash
pip install -e ".[dev]"
pytest tests/backend/
ruff check .
```

## コーディングスタイル

- Python: Ruff が import 順と一般的なバグを検出します
  (`B` / `F` / `E` ルールが有効)。インデントはスペース 4 つ。
- JavaScript: 素の JS、バンドラー不使用。UI コンポーネントは
  `window.MangaAutopilot.mountXyz(root, opts)` でマウントする形。

## リリース

- `pyproject.toml` のバージョンをバンプ
- CHANGELOG に新しいバージョンセクションを追加
- タグを打って push、メンテナが GitHub Release を作成

## 行動規範

優しく、現状より綺麗にしてから去ってください。
[GitHub コミュニティガイドライン](https://docs.github.com/ja/site-policy/github-terms/github-community-guidelines)
が適用されます。
