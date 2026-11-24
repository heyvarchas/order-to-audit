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
