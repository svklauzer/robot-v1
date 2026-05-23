class NewsFilter:
    BAD_WORDS = [
        "hack", "ban", "liquidation", "exploit", "lawsuit", "bankruptcy", "frozen"
    ]

    def classify(self, headlines: list[str]) -> dict:
        text = " ".join(headlines).lower()
        hits = [w for w in self.BAD_WORDS if w in text]

        if len(hits) >= 2:
            return {"state": "block_new_entries", "reasons": hits}
        if len(hits) == 1:
            return {"state": "caution", "reasons": hits}
        return {"state": "normal", "reasons": []}