"""ワーカーが 1 つのジョブに対して行う処理（変換 → AI レビュー → 結果）."""


class PermanentError(Exception):
    """やり直しても結果が変わらない失敗（入力の誤り・変換できない・設定の誤り）. ジョブを失敗にする."""


class RetryableError(Exception):
    """一時的な失敗（LLM の混雑・ネットワーク）. キューに戻して後でやり直す."""
