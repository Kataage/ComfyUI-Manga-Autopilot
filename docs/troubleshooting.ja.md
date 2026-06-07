# トラブルシューティング

> 🌐 **言語 / Language:** [English](troubleshooting.md) | **日本語** (このファイル)

よくある失敗パターンとその対処法をまとめます。

## ルートが登録されない

1. カスタムノードが ComfyUI の `sys.path` に含まれているか確認
   (`pip install -e .` を再実行)。
2. `WEB_DIRECTORY` がこのリポジトリの `web/` を指しているか確認。
3. ComfyUI のコンソールに `manga_autopilot` 関連のエラーが出ていないか確認。

## ヘルスチェックが失敗する

```bash
curl http://localhost:8188/manga_autopilot/api/health
```

200 以外が返る場合、カスタムノードがルートをアタッチできていません。
ComfyUI のコンソールに `aiohttp` のトレースバックが出力されていないか
確認してください。

## LLM 駆動のプランナーが 400 を返す

リペアループは、設定されたリトライ回数を使い切ると `ValueError` を
送出します。HTTP レスポンスはエラーを JSON シリアライズしたものです。
`max_repair_attempts` を増やすか、より軽量なモデル /
`manual` プロバイダに切り替えて手動で JSON を編集してください。

## コマの QA が失敗する

QA パイプラインは spec 18.4 の重み付け合計を使用します。
`quality_threshold` を下回ったコマは `RetryController` に送られ、
プロンプト修正やシード / ワークフローの差し替えが試みられます。
`{storage_root}/projects/{id}/qa_report.json` でチェックごとの
スコアを、`generation_log.json` でリトライ履歴を確認できます。

## 外部 GPU worker に接続できない

`GPUBridge` は失敗を `ExternalGPUClient` に記録し、
`GPUFallbackPolicy` にローカル ComfyUI サーバーへのフォールバックを
依頼します。フォールバックさせず明示的に失敗させたい場合は
`GPUFallbackPolicy.enabled = False` を設定してください。

## ストレージパス

ディスク上のレイアウトは `src/manga_autopilot/storage/paths.py` に
記載されています。`ensure_storage_root` / `ensure_project_paths` を
使ってパスを組み立てていれば、テスト環境と ComfyUI 統合の挙動が
一致します。

## テスト

```bash
pytest tests/backend/         # 400 件以上のテスト
ruff check .                  # スタイル + import 順のチェック
```

## それでも解決しない場合

<https://github.com/Kataage/ComfyUI-Manga-Autopilot/issues> で Issue を
作成してください。ComfyUI のコンソール出力と、可能であれば
`generation_log.json` / `qa_report.json` の内容を添付してください。
機密情報 (API キーなど) は必ず削除してください。
