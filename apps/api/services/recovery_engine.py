class RecoveryEngine:
    def reconcile(self, db_orders: list, exchange_orders: list, db_positions: list, exchange_positions: list) -> dict:
        mismatches = {
            "orders_missing_on_exchange": [],
            "orders_missing_in_db": [],
            "positions_missing_on_exchange": [],
            "positions_missing_in_db": [],
        }

        db_order_ids = {o.exchange_order_id for o in db_orders if o.exchange_order_id}
        ex_order_ids = {o["id"] for o in exchange_orders if o.get("id")}

        mismatches["orders_missing_on_exchange"] = list(db_order_ids - ex_order_ids)
        mismatches["orders_missing_in_db"] = list(ex_order_ids - db_order_ids)

        db_pos_symbols = {p.symbol for p in db_positions if p.status == "open"}
        ex_pos_symbols = {p.get("symbol") for p in exchange_positions if p.get("contracts") or p.get("contracts", 0) != 0}

        mismatches["positions_missing_on_exchange"] = list(db_pos_symbols - ex_pos_symbols)
        mismatches["positions_missing_in_db"] = list(ex_pos_symbols - db_pos_symbols)

        return mismatches