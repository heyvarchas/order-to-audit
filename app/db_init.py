"""Deterministic SQLite database initialization, foreign-key enforcement, and triggers."""

import sqlite3

import click
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.extensions import db

_SQLITE_FK_LISTENER_REGISTERED = False


def register_sqlite_foreign_keys():
    """Ensure PRAGMA foreign_keys = ON runs on every SQLite DBAPI connection."""
    global _SQLITE_FK_LISTENER_REGISTERED
    if _SQLITE_FK_LISTENER_REGISTERED:
        return

    @event.listens_for(Engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _SQLITE_FK_LISTENER_REGISTERED = True


SQLITE_TRIGGER_STATEMENTS = [
    # --- Audit log immutability ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update
    BEFORE UPDATE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs entries are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete
    BEFORE DELETE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs entries cannot be deleted');
    END
    """,
    # --- Stock ledger immutability ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_no_update
    BEFORE UPDATE ON stock_ledger
    BEGIN
        SELECT RAISE(ABORT, 'stock_ledger entries are immutable; post a reversing movement instead');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_no_delete
    BEFORE DELETE ON stock_ledger
    BEGIN
        SELECT RAISE(ABORT, 'stock_ledger entries cannot be deleted; post a reversing movement instead');
    END
    """,
    # --- Stock ledger polymorphic source validation ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_validate_source_doc
    BEFORE INSERT ON stock_ledger
    BEGIN
        SELECT RAISE(ABORT, 'Invalid stock_ledger source_doc_type')
        WHERE NEW.source_doc_type NOT IN (
            'stock_adjustment', 'purchase', 'production_run', 'dispatch'
        );

        SELECT RAISE(ABORT, 'Referenced stock_adjustment does not exist')
        WHERE NEW.source_doc_type = 'stock_adjustment'
          AND NOT EXISTS (
              SELECT 1 FROM stock_adjustments WHERE id = NEW.source_doc_id
          );

        SELECT RAISE(ABORT, 'Referenced purchase does not exist')
        WHERE NEW.source_doc_type = 'purchase'
          AND NOT EXISTS (
              SELECT 1 FROM purchases WHERE id = NEW.source_doc_id
          );

        SELECT RAISE(ABORT, 'Referenced production_run does not exist')
        WHERE NEW.source_doc_type = 'production_run'
          AND NOT EXISTS (
              SELECT 1 FROM production_runs WHERE id = NEW.source_doc_id
          );

        SELECT RAISE(ABORT, 'Referenced dispatch does not exist')
        WHERE NEW.source_doc_type = 'dispatch'
          AND NOT EXISTS (
              SELECT 1 FROM dispatches WHERE id = NEW.source_doc_id
          );
    END
    """,
    # --- Stock ledger purchase receipt validation ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_validate_purchase_receipt
    BEFORE INSERT ON stock_ledger
    WHEN NEW.source_doc_type = 'purchase' AND NEW.movement_type = 'purchase_receipt'
    BEGIN
        SELECT RAISE(ABORT, 'Purchase receipt ledger entry requires a posted purchase')
        WHERE NOT EXISTS (
            SELECT 1
            FROM purchases
            WHERE id = NEW.source_doc_id
              AND status = 'posted'
        );

        SELECT RAISE(ABORT, 'Purchase receipt ledger entry must match a purchase line item')
        WHERE NOT EXISTS (
            SELECT 1
            FROM purchase_items
            WHERE purchase_id = NEW.source_doc_id
              AND item_id = NEW.item_id
              AND qty_stock_milli = NEW.qty_change_milli
              AND taxable_value_paise = NEW.value_change_paise
        );
    END
    """,
    # --- Stock ledger reversal integrity ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_validate_reversal
    BEFORE INSERT ON stock_ledger
    WHEN NEW.movement_type IN ('reversal_in', 'reversal_out')
    BEGIN
        SELECT RAISE(ABORT, 'Reversal must reference an existing stock_ledger row')
        WHERE NEW.reverses_ledger_id IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM stock_ledger WHERE id = NEW.reverses_ledger_id
           );

        SELECT RAISE(ABORT, 'Cannot reverse a reversal ledger entry')
        WHERE EXISTS (
            SELECT 1
            FROM stock_ledger AS orig
            WHERE orig.id = NEW.reverses_ledger_id
              AND (
                  orig.movement_type IN ('reversal_in', 'reversal_out')
                  OR orig.reverses_ledger_id IS NOT NULL
              )
        );

        SELECT RAISE(ABORT, 'Reversal item_id must match original ledger entry')
        WHERE EXISTS (
            SELECT 1
            FROM stock_ledger AS orig
            WHERE orig.id = NEW.reverses_ledger_id
              AND NEW.item_id != orig.item_id
        );

        SELECT RAISE(ABORT, 'Reversal direction must oppose original movement sign')
        WHERE EXISTS (
            SELECT 1
            FROM stock_ledger AS orig
            WHERE orig.id = NEW.reverses_ledger_id
              AND (
                  (orig.qty_change_milli > 0 AND NEW.movement_type != 'reversal_out')
                  OR (orig.qty_change_milli < 0 AND NEW.movement_type != 'reversal_in')
              )
        );

        SELECT RAISE(ABORT, 'Reversal must exactly negate original qty_change_milli and value_change_paise')
        WHERE EXISTS (
            SELECT 1
            FROM stock_ledger AS orig
            WHERE orig.id = NEW.reverses_ledger_id
              AND (
                  NEW.qty_change_milli != -orig.qty_change_milli
                  OR NEW.value_change_paise != -orig.value_change_paise
                  OR NEW.unit_cost_paise != orig.unit_cost_paise
              )
        );
    END
    """,
    # --- Stock ledger non-negative chronological running balance protection ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_no_negative_balance
    AFTER INSERT ON stock_ledger
    BEGIN
        SELECT RAISE(ABORT, 'Stock ledger movement would result in negative inventory quantity')
        WHERE EXISTS (
            SELECT 1
            FROM (
                SELECT
                    SUM(qty_change_milli) OVER (
                        PARTITION BY item_id
                        ORDER BY movement_date ASC, id ASC
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS running_qty
                FROM stock_ledger
                WHERE item_id = NEW.item_id
            )
            WHERE running_qty < 0
        );

        SELECT RAISE(ABORT, 'Stock ledger movement would result in negative inventory value')
        WHERE EXISTS (
            SELECT 1
            FROM (
                SELECT
                    SUM(value_change_paise) OVER (
                        PARTITION BY item_id
                        ORDER BY movement_date ASC, id ASC
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS running_value
                FROM stock_ledger
                WHERE item_id = NEW.item_id
            )
            WHERE running_value < 0
        );
    END
    """,
    # --- Prevent deleting source documents referenced by stock_ledger ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_stock_adjustments_no_delete_if_referenced
    BEFORE DELETE ON stock_adjustments
    BEGIN
        SELECT RAISE(ABORT, 'Cannot delete stock_adjustment referenced by stock_ledger')
        WHERE EXISTS (
            SELECT 1 FROM stock_ledger
            WHERE source_doc_type = 'stock_adjustment' AND source_doc_id = OLD.id
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchases_no_delete_if_referenced
    BEFORE DELETE ON purchases
    BEGIN
        SELECT RAISE(ABORT, 'Cannot delete purchase referenced by stock_ledger')
        WHERE EXISTS (
            SELECT 1 FROM stock_ledger
            WHERE source_doc_type = 'purchase' AND source_doc_id = OLD.id
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_production_runs_no_delete_if_referenced
    BEFORE DELETE ON production_runs
    BEGIN
        SELECT RAISE(ABORT, 'Cannot delete production_run referenced by stock_ledger')
        WHERE EXISTS (
            SELECT 1 FROM stock_ledger
            WHERE source_doc_type = 'production_run' AND source_doc_id = OLD.id
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_dispatches_no_delete_if_referenced
    BEFORE DELETE ON dispatches
    BEGIN
        SELECT RAISE(ABORT, 'Cannot delete dispatch referenced by stock_ledger')
        WHERE EXISTS (
            SELECT 1 FROM stock_ledger
            WHERE source_doc_type = 'dispatch' AND source_doc_id = OLD.id
        );
    END
    """,
    # --- Purchase line draft-only & immutability triggers ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchase_items_insert_draft_only
    BEFORE INSERT ON purchase_items
    BEGIN
        SELECT RAISE(ABORT, 'Purchase lines can only be inserted while the parent purchase is in draft status')
        WHERE NOT EXISTS (
            SELECT 1 FROM purchases WHERE id = NEW.purchase_id AND status = 'draft'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchase_items_update_draft_only
    BEFORE UPDATE ON purchase_items
    BEGIN
        SELECT RAISE(ABORT, 'Purchase line cannot be moved to a different purchase')
        WHERE NEW.purchase_id != OLD.purchase_id;

        SELECT RAISE(ABORT, 'Purchase lines cannot be updated once the purchase is posted or cancelled')
        WHERE NOT EXISTS (
            SELECT 1 FROM purchases WHERE id = OLD.purchase_id AND status = 'draft'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchase_items_delete_draft_only
    BEFORE DELETE ON purchase_items
    BEGIN
        SELECT RAISE(ABORT, 'Purchase lines cannot be deleted once the purchase is posted or cancelled')
        WHERE EXISTS (
            SELECT 1 FROM purchases WHERE id = OLD.purchase_id AND status != 'draft'
        );
    END
    """,
    # --- Purchase header lifecycle, posting validation & immutability triggers ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchases_insert_draft_only
    BEFORE INSERT ON purchases
    BEGIN
        SELECT RAISE(ABORT, 'New purchases must start in draft status')
        WHERE NEW.status != 'draft';
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchases_no_delete_non_draft
    BEFORE DELETE ON purchases
    WHEN OLD.status != 'draft'
    BEGIN
        SELECT RAISE(ABORT, 'Posted or cancelled purchases cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_purchases_lifecycle_and_immutability
    BEFORE UPDATE ON purchases
    BEGIN
        SELECT RAISE(ABORT, 'Invalid purchase status transition')
        WHERE OLD.status != NEW.status
          AND NOT (
              (OLD.status = 'draft' AND NEW.status IN ('posted', 'cancelled'))
              OR (OLD.status = 'posted' AND NEW.status = 'cancelled')
          );

        SELECT RAISE(ABORT, 'Posted or cancelled purchase header fields are immutable')
        WHERE OLD.status != 'draft'
          AND (
              NEW.status = OLD.status
              OR NEW.id IS NOT OLD.id
              OR NEW.purchase_number IS NOT OLD.purchase_number
              OR NEW.supplier_id IS NOT OLD.supplier_id
              OR NEW.purchase_date IS NOT OLD.purchase_date
              OR NEW.supplier_invoice_no IS NOT OLD.supplier_invoice_no
              OR NEW.gst_type IS NOT OLD.gst_type
              OR NEW.subtotal_paise IS NOT OLD.subtotal_paise
              OR NEW.cgst_paise IS NOT OLD.cgst_paise
              OR NEW.sgst_paise IS NOT OLD.sgst_paise
              OR NEW.igst_paise IS NOT OLD.igst_paise
              OR NEW.total_paise IS NOT OLD.total_paise
              OR NEW.notes IS NOT OLD.notes
              OR NEW.created_by IS NOT OLD.created_by
              OR NEW.created_at IS NOT OLD.created_at
          );

        SELECT RAISE(ABORT, 'Cannot post a purchase without at least one line item')
        WHERE OLD.status = 'draft'
          AND NEW.status = 'posted'
          AND NOT EXISTS (
              SELECT 1 FROM purchase_items WHERE purchase_id = NEW.id
          );

        SELECT RAISE(ABORT, 'Purchase header totals do not match sum of purchase lines')
        WHERE OLD.status = 'draft'
          AND NEW.status = 'posted'
          AND EXISTS (
              SELECT 1
              FROM (
                  SELECT
                      COALESCE(SUM(taxable_value_paise), 0) AS sum_taxable,
                      COALESCE(SUM(cgst_paise), 0) AS sum_cgst,
                      COALESCE(SUM(sgst_paise), 0) AS sum_sgst,
                      COALESCE(SUM(igst_paise), 0) AS sum_igst,
                      COALESCE(SUM(line_total_paise), 0) AS sum_total
                  FROM purchase_items
                  WHERE purchase_id = NEW.id
              )
              WHERE NEW.subtotal_paise != sum_taxable
                 OR NEW.cgst_paise != sum_cgst
                 OR NEW.sgst_paise != sum_sgst
                 OR NEW.igst_paise != sum_igst
                 OR NEW.total_paise != sum_total
          );

        SELECT RAISE(ABORT, 'Purchase line GST values are inconsistent with purchase gst_type')
        WHERE OLD.status = 'draft'
          AND NEW.status = 'posted'
          AND EXISTS (
              SELECT 1
              FROM purchase_items AS pi
              WHERE pi.purchase_id = NEW.id
                AND (
                    (
                        NEW.gst_type = 'none'
                        AND (
                            pi.gst_rate_bps != 0
                            OR pi.cgst_paise != 0
                            OR pi.sgst_paise != 0
                            OR pi.igst_paise != 0
                        )
                    )
                    OR (
                        NEW.gst_type = 'intra_state'
                        AND (
                            pi.igst_paise != 0
                            OR pi.cgst_paise != ((pi.taxable_value_paise * pi.gst_rate_bps) + 10000) / 20000
                            OR pi.sgst_paise != ((pi.taxable_value_paise * pi.gst_rate_bps) + 10000) / 20000
                        )
                    )
                    OR (
                        NEW.gst_type = 'inter_state'
                        AND (
                            pi.cgst_paise != 0
                            OR pi.sgst_paise != 0
                            OR pi.igst_paise != ((pi.taxable_value_paise * pi.gst_rate_bps) + 5000) / 10000
                        )
                    )
                )
          );

        SELECT RAISE(ABORT, 'Cannot cancel a posted purchase until all purchase receipt ledger entries are reversed')
        WHERE OLD.status = 'posted'
          AND NEW.status = 'cancelled'
          AND (
              EXISTS (
                  SELECT 1
                  FROM purchase_items AS pi
                  WHERE pi.purchase_id = OLD.id
                    AND NOT EXISTS (
                        SELECT 1
                        FROM stock_ledger AS sl
                        WHERE sl.source_doc_type = 'purchase'
                          AND sl.source_doc_id = OLD.id
                          AND sl.movement_type = 'purchase_receipt'
                          AND sl.item_id = pi.item_id
                    )
              )
              OR EXISTS (
                  SELECT 1
                  FROM stock_ledger AS sl
                  WHERE sl.source_doc_type = 'purchase'
                    AND sl.source_doc_id = OLD.id
                    AND sl.movement_type = 'purchase_receipt'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM stock_ledger AS rev
                        WHERE rev.reverses_ledger_id = sl.id
                    )
              )
          );
    END
    """,
    # --- Invoice line draft-only & immutability triggers ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_invoice_items_insert_draft_only
    BEFORE INSERT ON invoice_items
    BEGIN
        SELECT RAISE(ABORT, 'Invoice lines can only be inserted while the parent invoice is in draft status')
        WHERE NOT EXISTS (
            SELECT 1 FROM invoices WHERE id = NEW.invoice_id AND status = 'draft'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_invoice_items_update_draft_only
    BEFORE UPDATE ON invoice_items
    BEGIN
        SELECT RAISE(ABORT, 'Invoice line cannot be moved to a different invoice')
        WHERE NEW.invoice_id != OLD.invoice_id;

        SELECT RAISE(ABORT, 'Invoice lines cannot be updated once the invoice is posted or credited')
        WHERE NOT EXISTS (
            SELECT 1 FROM invoices WHERE id = OLD.invoice_id AND status = 'draft'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_invoice_items_delete_draft_only
    BEFORE DELETE ON invoice_items
    BEGIN
        SELECT RAISE(ABORT, 'Invoice lines cannot be deleted once the invoice is posted or credited')
        WHERE NOT EXISTS (
            SELECT 1 FROM invoices WHERE id = OLD.invoice_id AND status = 'draft'
        );
    END
    """,
    # --- Invoice header posting validation & immutability triggers ---
    """
    CREATE TRIGGER IF NOT EXISTS trg_invoices_no_delete_posted
    BEFORE DELETE ON invoices
    WHEN OLD.status != 'draft'
    BEGIN
        SELECT RAISE(ABORT, 'Posted or credited invoices cannot be deleted; issue a credit note instead');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_invoices_lifecycle_and_immutability
    BEFORE UPDATE ON invoices
    BEGIN
        SELECT RAISE(ABORT, 'Invalid invoice status transition')
        WHERE NOT (
            (OLD.status = 'draft' AND NEW.status IN ('draft', 'posted'))
            OR (OLD.status = 'posted' AND NEW.status IN ('posted', 'credited'))
        );

        SELECT RAISE(ABORT, 'Cannot post an invoice without at least one line item')
        WHERE OLD.status = 'draft'
          AND NEW.status = 'posted'
          AND NOT EXISTS (
              SELECT 1 FROM invoice_items WHERE invoice_id = NEW.id
          );

        SELECT RAISE(ABORT, 'Invoice header totals do not match sum of invoice lines')
        WHERE OLD.status = 'draft'
          AND NEW.status = 'posted'
          AND EXISTS (
              SELECT 1
              FROM (
                  SELECT
                      COALESCE(SUM(taxable_value_paise), 0) AS sum_taxable,
                      COALESCE(SUM(cgst_paise), 0) AS sum_cgst,
                      COALESCE(SUM(sgst_paise), 0) AS sum_sgst,
                      COALESCE(SUM(igst_paise), 0) AS sum_igst,
                      COALESCE(SUM(line_total_paise), 0) AS sum_total
                  FROM invoice_items
                  WHERE invoice_id = NEW.id
              )
              WHERE NEW.taxable_value_paise != sum_taxable
                 OR NEW.cgst_paise != sum_cgst
                 OR NEW.sgst_paise != sum_sgst
                 OR NEW.igst_paise != sum_igst
                 OR NEW.total_paise != sum_total
          );

        SELECT RAISE(ABORT, 'Only a posted tax invoice with a posted credit note can be marked credited')
        WHERE OLD.status = 'posted'
          AND NEW.status = 'credited'
          AND (
              OLD.document_type != 'tax_invoice'
              OR NOT EXISTS (
                  SELECT 1
                  FROM invoices AS cn
                  WHERE cn.document_type = 'credit_note'
                    AND cn.original_invoice_id = OLD.id
                    AND cn.status = 'posted'
              )
          );

        SELECT RAISE(ABORT, 'Posted invoice header fields are immutable')
        WHERE OLD.status != 'draft'
          AND (
              NEW.id IS NOT OLD.id
              OR NEW.invoice_number IS NOT OLD.invoice_number
              OR NEW.financial_year IS NOT OLD.financial_year
              OR NEW.sequence_no IS NOT OLD.sequence_no
              OR NEW.document_type IS NOT OLD.document_type
              OR NEW.original_invoice_id IS NOT OLD.original_invoice_id
              OR NEW.sales_order_id IS NOT OLD.sales_order_id
              OR NEW.customer_id IS NOT OLD.customer_id
              OR NEW.customer_name_snapshot IS NOT OLD.customer_name_snapshot
              OR NEW.customer_gstin_snapshot IS NOT OLD.customer_gstin_snapshot
              OR NEW.invoice_date IS NOT OLD.invoice_date
              OR NEW.place_of_supply IS NOT OLD.place_of_supply
              OR NEW.gst_type IS NOT OLD.gst_type
              OR NEW.taxable_value_paise IS NOT OLD.taxable_value_paise
              OR NEW.cgst_paise IS NOT OLD.cgst_paise
              OR NEW.sgst_paise IS NOT OLD.sgst_paise
              OR NEW.igst_paise IS NOT OLD.igst_paise
              OR NEW.total_paise IS NOT OLD.total_paise
              OR NEW.created_by IS NOT OLD.created_by
              OR NEW.created_at IS NOT OLD.created_at
              OR NEW.posted_at IS NOT OLD.posted_at
          );
    END
    """,
]


def _install_sqlite_triggers():
    """Install all SQLite triggers idempotently."""
    with db.engine.begin() as conn:
        for stmt in SQLITE_TRIGGER_STATEMENTS:
            conn.execute(text(stmt))


def init_db(flask_app=None):
    """Deterministically create all tables, model indexes, and SQLite triggers.

    Safe to call multiple times (idempotent). Must be called inside an active
    Flask application context unless `flask_app` is passed explicitly.
    """
    # Ensure all SQLAlchemy models (and their partial unique indexes) are registered.
    from app import models  # noqa: F401

    register_sqlite_foreign_keys()

    if flask_app is not None:
        with flask_app.app_context():
            db.create_all()
            _install_sqlite_triggers()
    else:
        db.create_all()
        _install_sqlite_triggers()


def register_cli_commands(flask_app):
    """Register database CLI commands on the Flask application."""

    @flask_app.cli.command("init-db")
    def init_db_command():
        """Create all database tables, indexes, and SQLite triggers."""
        init_db()
        click.echo("Initialized the database.")
