import pytest

import services.nlm_service as nlm_service


@pytest.fixture(autouse=True)
def _reset_nlm_service_module_caches():
    """在每個測試前，重設 `nlm_service` 模組層級的所有快取。

    這些快取（session、sources、答案快取）都是模組層級的全域字典，
    生命週期跨越整個 pytest 行程、不會因為個別測試用了各自獨立的
    tmp_path 資料庫就自動清空。真實發生過的案例：不同測試檔案的
    fixture 常常重複使用同樣的字面常數（例如 channel_id="test-channel"、
    question="Q1"），一旦某個測試在答案快取裡留下一筆紀錄，同一次
    pytest 執行裡後面剛好撞到同樣 (channel_id, 問題文字) 組合的測試，
    就會意外命中別的測試留下的快取、完全沒有真的呼叫到 fake client，
    導致斷言 calls 數量的測試離奇失敗——而且失敗與否還會隨著測試
    執行順序改變，非常難以排查。每個測試各自的 `_setup()` 若有自行
    monkeypatch 這些快取，這裡的重設不影響它們，只是多一層保險，
    確保「忘記重設」不會再變成跨測試互相汙染的來源。"""
    nlm_service._client_cache.clear()
    nlm_service._sources_cache.clear()
    nlm_service._answer_cache.clear()
    yield
    nlm_service._client_cache.clear()
    nlm_service._sources_cache.clear()
    nlm_service._answer_cache.clear()
