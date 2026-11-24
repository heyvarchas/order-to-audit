"""Bill of Materials (BOM) management blueprint and routes."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.audit import log_audit
from app.csrf import require_csrf_token
from app.extensions import db
from app.models import BOM, BOMItem, Item, ProductionRun, Unit
from app.permissions import (
    CAP_MANAGE_PRODUCTS_CUSTOMERS,
    CAP_RECORD_PRODUCTION_RUNS,
    CAP_VIEW_FINANCIAL_REPORTS,
    roles_required,
    roles_with_permission,
)
from app.units import (
    UnitConversionError,
    convert_qty_milli_to_stock,
    format_milli,
    get_convertible_units_for_item,
    parse_qty_to_milli,
)

boms_bp = Blueprint("boms", __name__, url_prefix="/boms")

# Role groups derived directly from the centralized ROLE_PERMISSIONS matrix.
BOM_WRITE_ROLES = roles_with_permission(CAP_MANAGE_PRODUCTS_CUSTOMERS)
BOM_VIEW_ROLES = roles_with_permission(
    CAP_MANAGE_PRODUCTS_CUSTOMERS,
    CAP_RECORD_PRODUCTION_RUNS,
    CAP_VIEW_FINANCIAL_REPORTS,
)

MATERIAL_ITEM_TYPES = frozenset({"raw_material", "packaging"})
ITEM_TYPE_LABELS = {
    "raw_material": "Raw Material",
    "packaging": "Packaging Material",
    "finished_good": "Finished Good",
}


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _get_or_404(model, record_id: int):
    record = db.session.get(model, record_id)
    if record is None:
        abort(404)
    return record


def _is_bom_locked(bom_id: int) -> bool:
    """Return True if at least one ProductionRun references this BOM."""
    return (
        db.session.execute(
            db.select(ProductionRun.id)
            .where(ProductionRun.bom_id == bom_id)
            .limit(1)
        ).first()
        is not None
    )


def _next_version_for_item(finished_item_id: int) -> int:
    max_version = db.session.execute(
        db.select(func.max(BOM.version)).where(BOM.finished_item_id == finished_item_id)
    ).scalar()
    return (max_version or 0) + 1


def _bom_snapshot(bom: BOM) -> dict:
    return {
        "id": bom.id,
        "finished_item_id": bom.finished_item_id,
        "version": bom.version,
        "name": bom.name,
        "expected_yield_milli": bom.expected_yield_milli,
        "is_active": bom.is_active,
    }


def _bom_item_snapshot(bom_item: BOMItem, stock_qty_milli: int | None = None) -> dict:
    snap = {
        "bom_item_id": bom_item.id,
        "bom_id": bom_item.bom_id,
        "material_item_id": bom_item.material_item_id,
        "input_unit_id": bom_item.input_unit_id,
        "standard_qty_milli": bom_item.standard_qty_milli,
    }
    if stock_qty_milli is not None:
        snap["stock_qty_milli"] = stock_qty_milli
    return snap


def _validate_bom_create_form(form):
    """Validate BOM header creation form data."""
    finished_item_raw = (form.get("finished_item_id") or "").strip()
    name = (form.get("name") or "").strip()
    expected_yield_raw = (form.get("expected_yield") or "").strip()
    version_raw = (form.get("version") or "").strip()
    requested_active = (form.get("is_active") or "").strip() in ("1", "true", "on", "yes")

    values = {
        "finished_item_id": finished_item_raw,
        "name": name,
        "expected_yield": expected_yield_raw,
        "version": version_raw,
    }
    errors: dict[str, str] = {}

    finished_item = None
    if not finished_item_raw:
        errors["finished_item_id"] = "Finished product is required."
    else:
        try:
            finished_item = db.session.get(Item, int(finished_item_raw))
        except ValueError:
            finished_item = None

        if finished_item is None:
            errors["finished_item_id"] = "Selected finished product does not exist."
        elif finished_item.item_type != "finished_good":
            errors["finished_item_id"] = "BOMs can only be created for Finished Good items."
        elif not finished_item.is_active:
            errors["finished_item_id"] = "Cannot create a BOM for an inactive finished product."

    if not name:
        errors["name"] = "BOM name is required."
    elif len(name) > 150:
        errors["name"] = "BOM name must be 150 characters or fewer."

    expected_yield_milli, yield_err = parse_qty_to_milli(
        expected_yield_raw,
        field_label="Expected yield",
    )
    if yield_err:
        errors["expected_yield"] = yield_err

    version = None
    if version_raw:
        try:
            version = int(version_raw)
            if version <= 0:
                errors["version"] = "Version must be a positive whole number."
        except ValueError:
            errors["version"] = "Version must be a positive whole number."
    elif finished_item is not None:
        version = _next_version_for_item(finished_item.id)

    if finished_item is not None and version is not None and "version" not in errors:
        existing_version = db.session.execute(
            db.select(BOM.id).where(
                BOM.finished_item_id == finished_item.id,
                BOM.version == version,
            )
        ).scalar_one_or_none()
        if existing_version is not None:
            errors["version"] = (
                f"Version {version} already exists for '{finished_item.name}'."
            )

    if requested_active:
        errors["is_active"] = (
            "An empty BOM cannot be activated. Add components first, then activate the BOM."
        )

    cleaned = {
        "finished_item_id": finished_item.id if finished_item is not None else None,
        "version": version,
        "name": name,
        "expected_yield_milli": expected_yield_milli,
        "is_active": False,
    }
    return cleaned, errors, values


def _validate_bom_edit_form(form):
    """Validate BOM header edit form data (name and expected yield)."""
    name = (form.get("name") or "").strip()
    expected_yield_raw = (form.get("expected_yield") or "").strip()

    values = {
        "name": name,
        "expected_yield": expected_yield_raw,
    }
    errors: dict[str, str] = {}

    if not name:
        errors["name"] = "BOM name is required."
    elif len(name) > 150:
        errors["name"] = "BOM name must be 150 characters or fewer."

    expected_yield_milli, yield_err = parse_qty_to_milli(
        expected_yield_raw,
        field_label="Expected yield",
    )
    if yield_err:
        errors["expected_yield"] = yield_err

    cleaned = {
        "name": name,
        "expected_yield_milli": expected_yield_milli,
    }
    return cleaned, errors, values


def _validate_bom_item_form(
    form,
    *,
    bom: BOM,
    existing_bom_item: BOMItem | None = None,
):
    """Validate adding or editing a BOM component line."""
    material_raw = (
        str(existing_bom_item.material_item_id)
        if existing_bom_item is not None
        else (form.get("material_item_id") or "").strip()
    )
    input_unit_raw = (form.get("input_unit_id") or "").strip()
    qty_raw = (form.get("standard_qty") or "").strip()

    values = {
        "material_item_id": material_raw,
        "input_unit_id": input_unit_raw,
        "standard_qty": qty_raw,
    }
    errors: dict[str, str] = {}

    material_item = existing_bom_item.material_item if existing_bom_item is not None else None
    if material_item is None:
        if not material_raw:
            errors["material_item_id"] = "Component material is required."
        else:
            try:
                material_item = db.session.get(Item, int(material_raw))
            except ValueError:
                material_item = None

            if material_item is None:
                errors["material_item_id"] = "Selected component material does not exist."
            elif material_item.id == bom.finished_item_id:
                errors["material_item_id"] = "A finished product cannot be a component of its own BOM."
            elif material_item.item_type not in MATERIAL_ITEM_TYPES:
                errors["material_item_id"] = (
                    "Only Raw Material or Packaging Material items can be added to a BOM."
                )
            elif not material_item.is_active:
                errors["material_item_id"] = "Cannot add an inactive material item to a BOM."
            else:
                duplicate = db.session.execute(
                    db.select(BOMItem.id).where(
                        BOMItem.bom_id == bom.id,
                        BOMItem.material_item_id == material_item.id,
                    )
                ).scalar_one_or_none()
                if duplicate is not None:
                    errors["material_item_id"] = (
                        f"'{material_item.name}' is already listed in this BOM."
                    )

    input_unit = None
    if not input_unit_raw:
        errors["input_unit_id"] = "Unit is required."
    else:
        try:
            input_unit = db.session.get(Unit, int(input_unit_raw))
        except ValueError:
            input_unit = None
        if input_unit is None:
            errors["input_unit_id"] = "Selected unit does not exist."

    standard_qty_milli, qty_err = parse_qty_to_milli(
        qty_raw,
        field_label="Planned quantity",
    )
    if qty_err:
        errors["standard_qty"] = qty_err

    stock_qty_milli = None
    if (
        material_item is not None
        and input_unit is not None
        and standard_qty_milli is not None
        and "material_item_id" not in errors
    ):
        try:
            stock_qty_milli = convert_qty_milli_to_stock(
                material_item,
                standard_qty_milli,
                input_unit,
            )
        except UnitConversionError as exc:
            errors["input_unit_id"] = str(exc)

    cleaned = {
        "material_item_id": material_item.id if material_item is not None else None,
        "input_unit_id": input_unit.id if input_unit is not None else None,
        "standard_qty_milli": standard_qty_milli,
        "stock_qty_milli": stock_qty_milli,
    }
    return cleaned, errors, values


def _build_bom_detail_context(
    bom: BOM,
    *,
    item_values: dict | None = None,
    item_errors: dict | None = None,
) -> dict:
    """Assemble template context for `boms/bom_detail.html`."""
    is_locked = _is_bom_locked(bom.id)

    sorted_items = sorted(
        bom.items,
        key=lambda bi: (bi.material_item.item_type, bi.material_item.name.lower(), bi.id),
    )

    line_rows = []
    for bom_item in sorted_items:
        stock_qty_milli = None
        conv_error = None
        try:
            stock_qty_milli = convert_qty_milli_to_stock(
                bom_item.material_item,
                bom_item.standard_qty_milli,
                bom_item.input_unit,
            )
        except UnitConversionError as exc:
            conv_error = str(exc)

        try:
            convertible_units = get_convertible_units_for_item(bom_item.material_item)
        except UnitConversionError as exc:
            convertible_units = []
            if conv_error is None:
                conv_error = str(exc)
        if bom_item.input_unit not in convertible_units:
            convertible_units = [*convertible_units, bom_item.input_unit]

        line_rows.append(
            {
                "bom_item": bom_item,
                "stock_qty_milli": stock_qty_milli,
                "conversion_error": conv_error,
                "convertible_units": convertible_units,
            }
        )

    existing_material_ids = {bi.material_item_id for bi in bom.items}
    material_items = db.session.execute(
        db.select(Item)
        .where(
            Item.item_type.in_(MATERIAL_ITEM_TYPES),
            Item.is_active.is_(True),
        )
        .order_by(Item.item_type.asc(), Item.name.asc(), Item.sku.asc())
    ).scalars().all()
    available_materials = [m for m in material_items if m.id not in existing_material_ids]

    all_units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    other_versions = db.session.execute(
        db.select(BOM)
        .where(BOM.finished_item_id == bom.finished_item_id, BOM.id != bom.id)
        .order_by(BOM.version.desc())
    ).scalars().all()

    return {
        "bom": bom,
        "is_locked": is_locked,
        "line_rows": line_rows,
        "available_materials": available_materials,
        "all_units": all_units,
        "other_versions": other_versions,
        "item_type_labels": ITEM_TYPE_LABELS,
        "item_values": item_values
        or {"material_item_id": "", "input_unit_id": "", "standard_qty": ""},
        "item_errors": item_errors or {},
        "format_milli": format_milli,
    }


# ---------------------------------------------------------------------------
# BOM Routes
# ---------------------------------------------------------------------------


@boms_bp.route("")
@roles_required(*BOM_VIEW_ROLES)
def list_boms():
    finished_item_filter = (request.args.get("finished_item_id") or "").strip()
    status_filter = (request.args.get("status") or "all").strip()

    query = (
        db.select(BOM)
        .join(Item, BOM.finished_item_id == Item.id)
        .order_by(Item.name.asc(), BOM.version.desc())
    )

    if finished_item_filter:
        try:
            query = query.where(BOM.finished_item_id == int(finished_item_filter))
        except ValueError:
            finished_item_filter = ""

    if status_filter == "active":
        query = query.where(BOM.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.where(BOM.is_active.is_(False))
    else:
        status_filter = "all"

    boms = db.session.execute(query).scalars().all()

    finished_items = db.session.execute(
        db.select(Item)
        .where(Item.item_type == "finished_good")
        .order_by(Item.name.asc(), Item.sku.asc())
    ).scalars().all()

    bom_ids = [b.id for b in boms]
    locked_bom_ids: set[int] = set()
    if bom_ids:
        locked_bom_ids = set(
            db.session.execute(
                db.select(ProductionRun.bom_id)
                .where(ProductionRun.bom_id.in_(bom_ids))
                .distinct()
            ).scalars().all()
        )

    return render_template(
        "boms/boms_list.html",
        boms=boms,
        finished_items=finished_items,
        finished_item_filter=finished_item_filter,
        status_filter=status_filter,
        locked_bom_ids=locked_bom_ids,
        format_milli=format_milli,
    )


@boms_bp.route("/new", methods=["GET", "POST"])
@roles_required(*BOM_WRITE_ROLES)
def create_bom():
    finished_items = db.session.execute(
        db.select(Item)
        .where(Item.item_type == "finished_good", Item.is_active.is_(True))
        .order_by(Item.name.asc(), Item.sku.asc())
    ).scalars().all()

    values = {
        "finished_item_id": (request.args.get("finished_item_id") or "").strip(),
        "name": "",
        "expected_yield": "",
        "version": "",
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_bom_create_form(request.form)
        if not errors:
            bom = BOM(**cleaned)
            db.session.add(bom)
            try:
                db.session.flush()
                log_audit(
                    user_id=current_user.id,
                    action="bom_created",
                    entity_type="bom",
                    entity_id=bom.id,
                    summary=f"Created BOM '{bom.name}' (v{bom.version}) for item #{bom.finished_item_id}",
                    new_values=_bom_snapshot(bom),
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["version"] = (
                    "Could not create BOM because this version already exists for the finished product."
                )
            else:
                flash(
                    f"BOM '{bom.name}' (v{bom.version}) created as inactive. "
                    f"Add components and activate it when ready.",
                    "success",
                )
                return redirect(url_for("boms.view_bom", bom_id=bom.id))

    return render_template(
        "boms/bom_form.html",
        mode="create",
        bom=None,
        values=values,
        errors=errors,
        finished_items=finished_items,
    )


@boms_bp.route("/<int:bom_id>")
@roles_required(*BOM_VIEW_ROLES)
def view_bom(bom_id: int):
    bom = _get_or_404(BOM, bom_id)
    return render_template("boms/bom_detail.html", **_build_bom_detail_context(bom))


@boms_bp.route("/<int:bom_id>/edit", methods=["GET", "POST"])
@roles_required(*BOM_WRITE_ROLES)
def edit_bom(bom_id: int):
    bom = _get_or_404(BOM, bom_id)
    if _is_bom_locked(bom.id):
        if request.method == "POST":
            require_csrf_token()
        flash(
            "This BOM is referenced by production runs and cannot be edited. "
            "Create a new version instead.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    values = {
        "name": bom.name,
        "expected_yield": format_milli(bom.expected_yield_milli),
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_bom_edit_form(request.form)
        if not errors:
            old_values = _bom_snapshot(bom)
            bom.name = cleaned["name"]
            bom.expected_yield_milli = cleaned["expected_yield_milli"]
            try:
                log_audit(
                    user_id=current_user.id,
                    action="bom_updated",
                    entity_type="bom",
                    entity_id=bom.id,
                    summary=f"Updated BOM '{bom.name}' (v{bom.version})",
                    old_values=old_values,
                    new_values=_bom_snapshot(bom),
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Could not update BOM due to a database constraint error.", "danger")
                return redirect(url_for("boms.view_bom", bom_id=bom.id))
            else:
                flash(f"BOM '{bom.name}' (v{bom.version}) updated.", "success")
                return redirect(url_for("boms.view_bom", bom_id=bom.id))

    return render_template(
        "boms/bom_form.html",
        mode="edit",
        bom=bom,
        values=values,
        errors=errors,
        finished_items=[],
    )


@boms_bp.route("/<int:bom_id>/activate", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def activate_bom(bom_id: int):
    require_csrf_token()
    bom = _get_or_404(BOM, bom_id)

    if bom.is_active:
        flash(f"BOM '{bom.name}' (v{bom.version}) is already active.", "info")
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    if not bom.items:
        flash(
            "Cannot activate an empty BOM. Add at least one component first.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    if not bom.finished_item.is_active:
        flash(
            "Cannot activate a BOM for an inactive finished product.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    inactive_components = [
        f"{bi.material_item.name} ({bi.material_item.sku})"
        for bi in sorted(
            bom.items,
            key=lambda bi: (bi.material_item.name.lower(), bi.id),
        )
        if not bi.material_item.is_active
    ]
    if inactive_components:
        flash(
            "Cannot activate BOM because the following component item(s) are inactive: "
            f"{', '.join(inactive_components)}.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    for bom_item in bom.items:
        try:
            convert_qty_milli_to_stock(
                bom_item.material_item,
                bom_item.standard_qty_milli,
                bom_item.input_unit,
            )
        except UnitConversionError as exc:
            flash(f"Cannot activate BOM: {exc}", "danger")
            return redirect(url_for("boms.view_bom", bom_id=bom.id))

    other_active_boms = db.session.execute(
        db.select(BOM).where(
            BOM.finished_item_id == bom.finished_item_id,
            BOM.id != bom.id,
            BOM.is_active.is_(True),
        )
    ).scalars().all()

    deactivated_versions = []
    try:
        for other in other_active_boms:
            old_other = _bom_snapshot(other)
            other.is_active = False
            deactivated_versions.append(f"v{other.version}")
            log_audit(
                user_id=current_user.id,
                action="bom_deactivated",
                entity_type="bom",
                entity_id=other.id,
                summary=(
                    f"Deactivated BOM '{other.name}' (v{other.version}) upon activating "
                    f"BOM #{bom.id} (v{bom.version})"
                ),
                old_values=old_other,
                new_values=_bom_snapshot(other),
            )

        old_target = _bom_snapshot(bom)
        bom.is_active = True
        log_audit(
            user_id=current_user.id,
            action="bom_activated",
            entity_type="bom",
            entity_id=bom.id,
            summary=f"Activated BOM '{bom.name}' (v{bom.version})",
            old_values=old_target,
            new_values={
                **_bom_snapshot(bom),
                "deactivated_bom_ids": [o.id for o in other_active_boms],
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Failed to activate BOM. No changes were saved.", "danger")
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    if deactivated_versions:
        flash(
            f"BOM '{bom.name}' (v{bom.version}) activated. "
            f"Deactivated previous active version(s): {', '.join(deactivated_versions)}.",
            "success",
        )
    else:
        flash(f"BOM '{bom.name}' (v{bom.version}) activated.", "success")
    return redirect(url_for("boms.view_bom", bom_id=bom.id))


@boms_bp.route("/<int:bom_id>/deactivate", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def deactivate_bom(bom_id: int):
    require_csrf_token()
    bom = _get_or_404(BOM, bom_id)

    if not bom.is_active:
        flash(f"BOM '{bom.name}' (v{bom.version}) is already inactive.", "info")
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    old_values = _bom_snapshot(bom)
    try:
        bom.is_active = False
        log_audit(
            user_id=current_user.id,
            action="bom_deactivated",
            entity_type="bom",
            entity_id=bom.id,
            summary=f"Deactivated BOM '{bom.name}' (v{bom.version})",
            old_values=old_values,
            new_values=_bom_snapshot(bom),
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Failed to deactivate BOM. No changes were saved.", "danger")
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    flash(f"BOM '{bom.name}' (v{bom.version}) deactivated.", "info")
    return redirect(url_for("boms.view_bom", bom_id=bom.id))


@boms_bp.route("/<int:bom_id>/new-version", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def clone_bom_version(bom_id: int):
    require_csrf_token()
    source_bom = _get_or_404(BOM, bom_id)

    next_version = _next_version_for_item(source_bom.finished_item_id)
    new_bom = BOM(
        finished_item_id=source_bom.finished_item_id,
        version=next_version,
        name=source_bom.name,
        expected_yield_milli=source_bom.expected_yield_milli,
        is_active=False,
    )
    db.session.add(new_bom)

    try:
        db.session.flush()
        cloned_items = []
        for line in source_bom.items:
            cloned_line = BOMItem(
                bom_id=new_bom.id,
                material_item_id=line.material_item_id,
                input_unit_id=line.input_unit_id,
                standard_qty_milli=line.standard_qty_milli,
            )
            db.session.add(cloned_line)
            cloned_items.append(cloned_line)

        db.session.flush()
        log_audit(
            user_id=current_user.id,
            action="bom_version_cloned",
            entity_type="bom",
            entity_id=new_bom.id,
            summary=(
                f"Cloned BOM '{source_bom.name}' v{source_bom.version} to "
                f"v{new_bom.version} (inactive)"
            ),
            old_values={
                "source_bom_id": source_bom.id,
                "source_version": source_bom.version,
            },
            new_values={
                **_bom_snapshot(new_bom),
                "component_count": len(cloned_items),
            },
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Could not create a new BOM version due to a version conflict.", "danger")
        return redirect(url_for("boms.view_bom", bom_id=source_bom.id))

    flash(
        f"Created BOM version v{new_bom.version} (inactive) cloned from v{source_bom.version}. "
        f"Activate it explicitly when ready.",
        "success",
    )
    return redirect(url_for("boms.view_bom", bom_id=new_bom.id))


@boms_bp.route("/<int:bom_id>/delete", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def delete_bom(bom_id: int):
    require_csrf_token()
    bom = _get_or_404(BOM, bom_id)

    if _is_bom_locked(bom.id):
        flash(
            "This BOM is referenced by production runs and cannot be deleted.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    if bom.is_active:
        flash(
            "Deactivate this BOM before deleting it.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    old_values = {
        **_bom_snapshot(bom),
        "components": [_bom_item_snapshot(bi) for bi in bom.items],
    }
    bom_name = bom.name
    bom_version = bom.version

    try:
        db.session.delete(bom)
        log_audit(
            user_id=current_user.id,
            action="bom_deleted",
            entity_type="bom",
            entity_id=bom_id,
            summary=f"Deleted unused BOM '{bom_name}' (v{bom_version})",
            old_values=old_values,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Cannot delete this BOM because it is referenced by other records.", "danger")
        return redirect(url_for("boms.view_bom", bom_id=bom_id))

    flash(f"BOM '{bom_name}' (v{bom_version}) deleted.", "info")
    return redirect(url_for("boms.list_boms"))


@boms_bp.route("/<int:bom_id>/items", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def add_bom_item(bom_id: int):
    require_csrf_token()
    bom = _get_or_404(BOM, bom_id)

    if _is_bom_locked(bom.id):
        flash(
            "This BOM is referenced by production runs and its components cannot be modified. "
            "Create a new version instead.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    cleaned, errors, values = _validate_bom_item_form(request.form, bom=bom)
    if not errors:
        bom_item = BOMItem(
            bom_id=bom.id,
            material_item_id=cleaned["material_item_id"],
            input_unit_id=cleaned["input_unit_id"],
            standard_qty_milli=cleaned["standard_qty_milli"],
        )
        db.session.add(bom_item)
        try:
            db.session.flush()
            log_audit(
                user_id=current_user.id,
                action="bom_item_added",
                entity_type="bom",
                entity_id=bom.id,
                summary=(
                    f"Added component item #{bom_item.material_item_id} "
                    f"({format_milli(bom_item.standard_qty_milli)} unit #{bom_item.input_unit_id}) "
                    f"to BOM '{bom.name}' (v{bom.version})"
                ),
                new_values=_bom_item_snapshot(bom_item, cleaned["stock_qty_milli"]),
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            errors["material_item_id"] = (
                "Could not add component because this material is already in the BOM or invalid."
            )
        else:
            flash("Component added to BOM.", "success")
            return redirect(url_for("boms.view_bom", bom_id=bom.id))

    return render_template(
        "boms/bom_detail.html",
        **_build_bom_detail_context(bom, item_values=values, item_errors=errors),
    )


@boms_bp.route("/<int:bom_id>/items/<int:bom_item_id>/edit", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def edit_bom_item(bom_id: int, bom_item_id: int):
    require_csrf_token()
    bom = _get_or_404(BOM, bom_id)
    bom_item = _get_or_404(BOMItem, bom_item_id)
    if bom_item.bom_id != bom.id:
        abort(404)

    if _is_bom_locked(bom.id):
        flash(
            "This BOM is referenced by production runs and its components cannot be edited. "
            "Create a new version instead.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    cleaned, errors, _values = _validate_bom_item_form(
        request.form,
        bom=bom,
        existing_bom_item=bom_item,
    )
    if errors:
        flash(next(iter(errors.values())), "danger")
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    old_values = _bom_item_snapshot(bom_item)
    bom_item.input_unit_id = cleaned["input_unit_id"]
    bom_item.standard_qty_milli = cleaned["standard_qty_milli"]

    try:
        log_audit(
            user_id=current_user.id,
            action="bom_item_updated",
            entity_type="bom",
            entity_id=bom.id,
            summary=(
                f"Updated component '{bom_item.material_item.name}' in "
                f"BOM '{bom.name}' (v{bom.version})"
            ),
            old_values=old_values,
            new_values=_bom_item_snapshot(bom_item, cleaned["stock_qty_milli"]),
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Could not update component due to a database constraint error.", "danger")
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    flash(f"Component '{bom_item.material_item.name}' updated.", "success")
    return redirect(url_for("boms.view_bom", bom_id=bom.id))


@boms_bp.route("/<int:bom_id>/items/<int:bom_item_id>/delete", methods=["POST"])
@roles_required(*BOM_WRITE_ROLES)
def delete_bom_item(bom_id: int, bom_item_id: int):
    require_csrf_token()
    bom = _get_or_404(BOM, bom_id)
    bom_item = _get_or_404(BOMItem, bom_item_id)
    if bom_item.bom_id != bom.id:
        abort(404)

    if _is_bom_locked(bom.id):
        flash(
            "This BOM is referenced by production runs and its components cannot be deleted. "
            "Create a new version instead.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    if bom.is_active and len(bom.items) <= 1:
        flash(
            "Cannot remove the last component from an active BOM. "
            "Deactivate the BOM first or add another component.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    old_values = _bom_item_snapshot(bom_item)
    material_name = bom_item.material_item.name
    try:
        db.session.delete(bom_item)
        log_audit(
            user_id=current_user.id,
            action="bom_item_removed",
            entity_type="bom",
            entity_id=bom.id,
            summary=f"Removed component '{material_name}' from BOM '{bom.name}' (v{bom.version})",
            old_values=old_values,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            f"Failed to remove component '{material_name}' from BOM. No changes were saved.",
            "danger",
        )
        return redirect(url_for("boms.view_bom", bom_id=bom.id))

    flash(f"Component '{material_name}' removed from BOM.", "info")
    return redirect(url_for("boms.view_bom", bom_id=bom.id))
