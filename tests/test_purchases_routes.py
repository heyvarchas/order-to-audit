"""Step B — Step 3B route tests: Purchase UI, filters, draft line management, posting, cancellation, CSRF, RBAC & traceability."""

from datetime import date

import pytest

from app.csrf import CSRF_SESSION_KEY
from app.extensions import db
from app.inventory_service import (
    get_item_stock_position,
    post_stock_issue,
)
from app.models import (
    AuditLog,
    Item,
    ItemUnitConversion,
    Purchase,
    StockAdjustment,
    StockLedger,
    Supplier,
    Unit,
    User,
)
from app.permissions import (
    CAP_VIEW_FINANCIAL_REPORTS,
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    ROLE_PRODUCTION_SUPERVISOR,
    ROLE_SALESMAN,
)
from app.purchase_service import (
    PurchaseLineInput,
    create_draft_purchase,
    get_business_today,
    post_purchase,
)


def _create_user(
    db_session,
    *,
    username: str,
    role: str,
    password: str = "secret-pass",
) -> User:
    user = User(
        username=username,
        full_name=f"{username.title()} User",
        email=f"{username}@example.com",
        role=role,
        is_active=True,
    )
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        return sess[CSRF_SESSION_KEY]


def _login(client, username: str, password: str = "secret-pass"):
    client.get("/login")
    res = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _csrf(client),
        },
    )
    assert res.status_code == 302
    return res


def _seed_master_data(db_session):
    """Seed units, item-specific conversions, items of all 3 types, and suppliers."""
    g = Unit(code="g", name="Gram", dimension="mass", factor_to_base=1)
    ml = Unit(code="ml", name="Millilitre", dimension="volume", factor_to_base=1)
    piece = Unit(code="piece", name="Piece", dimension="count", factor_to_base=1)
    packet = Unit(code="packet", name="Packet", dimension="count", factor_to_base=1)
    carton = Unit(code="carton", name="Carton", dimension="count", factor_to_base=1)
    bag = Unit(code="bag", name="Bag", dimension="mass", factor_to_base=1)
    db_session.add_all([g, ml, piece, packet, carton, bag])
    db_session.flush()

    kg = Unit(code="kg", name="Kilogram", dimension="mass", base_unit_id=g.id, factor_to_base=1000)
    litre = Unit(code="L", name="Litre", dimension="volume", base_unit_id=ml.id, factor_to_base=1000)
    db_session.add_all([kg, litre])
    db_session.flush()

    flour = Item(
        sku="RM-FLOUR",
        name="Flour",
        item_type="raw_material",
        stock_unit_id=kg.id,
        hsn_code="11010000",
        gst_rate_bps=500,
        is_active=True,
    )
    oil = Item(
        sku="RM-OIL",
        name="Groundnut Oil",
        item_type="raw_material",
        stock_unit_id=litre.id,
        hsn_code="15089091",
        gst_rate_bps=500,
        is_active=True,
    )
    pouches = Item(
        sku="PKG-POUCH-200G",
        name="200g Pouches",
        item_type="packaging",
        stock_unit_id=piece.id,
        hsn_code="39232100",
        gst_rate_bps=1800,
        is_active=True,
    )
    murukku = Item(
        sku="FG-MURUKKU-200G",
        name="Murukku 200 g",
        item_type="finished_good",
        stock_unit_id=packet.id,
        hsn_code="21069099",
        gst_rate_bps=1200,
        default_sale_price_paise=5000,
        is_active=True,
    )
    inactive_item = Item(
        sku="RM-OLD",
        name="Discontinued Spice",
        item_type="raw_material",
        stock_unit_id=kg.id,
        gst_rate_bps=500,
        is_active=False,
    )

    supplier_tn = Supplier(
        name="Tamil Nadu Millers",
        gstin="33AAAAA0000A1Z5",
        state="Tamil Nadu",
        is_active=True,
    )
    supplier_ka = Supplier(
        name="Karnataka Oils Ltd",
        gstin="29BBBBB0000B1Z5",
        state="Karnataka",
        is_active=True,
    )
    inactive_supplier = Supplier(
        name="Closed Vendor",
        state="Tamil Nadu",
        is_active=False,
    )

    db_session.add_all(
        [
            flour,
            oil,
            pouches,
            murukku,
            inactive_item,
            supplier_tn,
            supplier_ka,
            inactive_supplier,
        ]
    )
    db_session.flush()

    db_session.add_all(
        [
            ItemUnitConversion(
                item_id=flour.id,
                from_unit_id=bag.id,
                to_unit_id=kg.id,
                multiplier=25,
            ),
            ItemUnitConversion(
                item_id=murukku.id,
                from_unit_id=carton.id,
                to_unit_id=packet.id,
                multiplier=20,
            ),
        ]
    )
    db_session.commit()

    return {
        "units": {
            "g": g,
            "kg": kg,
            "ml": ml,
            "L": litre,
            "piece": piece,
            "packet": packet,
            "carton": carton,
            "bag": bag,
        },
        "items": {
            "flour": flour,
            "oil": oil,
            "pouches": pouches,
            "murukku": murukku,
            "inactive": inactive_item,
        },
        "suppliers": {
            "tn": supplier_tn,
            "ka": supplier_ka,
            "inactive": inactive_supplier,
        },
    }


# ---------------------------------------------------------------------------
# 1. Authentication & Five-Role RBAC Matrix + UI Visibility
# ---------------------------------------------------------------------------


def test_unauthenticated_purchase_routes_redirect_to_login(client, db_session):
    admin = _create_user(db_session, username="seed_admin_unauth", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=admin.id,
    )

    for path in (
        "/purchases",
        "/purchases/new",
        f"/purchases/{draft.id}",
        f"/purchases/{draft.id}/edit",
    ):
        res = client.get(path)
        assert res.status_code == 302
        assert "/login" in res.headers["Location"]


@pytest.mark.parametrize(
    "role,can_read,can_write,can_link_supplier",
    [
        (ROLE_ADMIN, True, True, True),
        (ROLE_OWNER, True, True, True),
        (ROLE_ACCOUNTANT, True, False, False),
        (ROLE_PRODUCTION_SUPERVISOR, False, False, False),
        (ROLE_SALESMAN, False, False, False),
    ],
)
def test_purchases_rbac_and_navigation_visibility_all_five_roles(
    client, db_session, role, can_read, can_write, can_link_supplier
):
    seed_admin = _create_user(db_session, username="seed_admin_rbac_rt", role=ROLE_ADMIN)
    _create_user(db_session, username=f"user_{role}", role=role)
    data = _seed_master_data(db_session)
    supplier = data["suppliers"]["tn"]
    flour = data["items"]["flour"]
    oil = data["items"]["oil"]

    draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=seed_admin.id,
        gst_type="intra_state",
        supplier_invoice_no="INV-RBAC-1",
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    line_id = draft.items[0].id

    _login(client, f"user_{role}")

    # Navbar visibility on home page
    home = client.get("/")
    assert home.status_code == 200
    if can_read:
        assert b'href="/purchases"' in home.data
    else:
        assert b'href="/purchases"' not in home.data

    # List & Detail GET routes
    list_res = client.get("/purchases")
    detail_res = client.get(f"/purchases/{draft.id}")
    assert list_res.status_code == (200 if can_read else 403)
    assert detail_res.status_code == (200 if can_read else 403)

    if can_read:
        assert draft.purchase_number.encode() in list_res.data
        assert draft.purchase_number.encode() in detail_res.data
        assert b"420.00" in detail_res.data  # taxable subtotal
        assert b"10.50" in detail_res.data  # CGST / SGST (2.5% each)
        assert b"441.00" in detail_res.data  # grand total

        supplier_href = f'/master/suppliers/{supplier.id}'.encode()
        if can_link_supplier:
            assert supplier_href in list_res.data
            assert supplier_href in detail_res.data
        else:
            assert supplier.name.encode() in list_res.data
            assert supplier.name.encode() in detail_res.data
            assert supplier_href not in list_res.data
            assert supplier_href not in detail_res.data

        if can_write:
            assert b"+ New Purchase" in list_res.data
            assert b"Edit Header" in detail_res.data
            assert b"Post Purchase" in detail_res.data
            assert b"Cancel Draft" in detail_res.data
            assert b"Add Line Item" in detail_res.data
        else:
            assert b"+ New Purchase" not in list_res.data
            assert b"Edit Header" not in detail_res.data
            assert b"Post Purchase" not in detail_res.data
            assert b"Cancel Draft" not in detail_res.data
            assert b"Add Line Item" not in detail_res.data

    # Write GET routes
    assert client.get("/purchases/new").status_code == (200 if can_write else 403)
    assert client.get(f"/purchases/{draft.id}/edit").status_code == (200 if can_write else 403)

    # Write POST routes (only assert 403 for non-writers so draft stays intact)
    if not can_write:
        token = _csrf(client)
        assert (
            client.post(
                "/purchases/new",
                data={
                    "csrf_token": token,
                    "supplier_id": str(supplier.id),
                    "purchase_date": "2026-11-20",
                    "gst_type": "none",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/purchases/{draft.id}/edit",
                data={
                    "csrf_token": token,
                    "supplier_id": str(supplier.id),
                    "purchase_date": "2026-11-20",
                    "gst_type": "none",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/purchases/{draft.id}/items",
                data={
                    "csrf_token": token,
                    "item_id": str(oil.id),
                    "entered_qty": "5",
                    "unit_rate": "140.00",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/purchases/{draft.id}/items/{line_id}/edit",
                data={
                    "csrf_token": token,
                    "entered_qty": "20",
                    "unit_rate": "42.00",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/purchases/{draft.id}/items/{line_id}/delete",
                data={"csrf_token": token},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/purchases/{draft.id}/post",
                data={"csrf_token": token},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/purchases/{draft.id}/cancel",
                data={"csrf_token": token, "reason": "Unauthorized"},
            ).status_code
            == 403
        )


# ---------------------------------------------------------------------------
# 2. CSRF Protection Across Every POST Route
# ---------------------------------------------------------------------------


def test_all_purchase_post_routes_reject_missing_or_invalid_csrf(client, db_session):
    admin = _create_user(db_session, username="admin_csrf", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=admin.id,
        lines=[
            PurchaseLineInput(
                item_id=data["items"]["flour"].id,
                entered_qty_milli=10_000,
                unit_rate_paise=4200,
            )
        ],
    )
    line_id = draft.items[0].id

    _login(client, "admin_csrf")

    post_cases = [
        (
            "/purchases/new",
            {
                "supplier_id": str(data["suppliers"]["tn"].id),
                "purchase_date": "2026-11-20",
                "gst_type": "none",
            },
        ),
        (
            f"/purchases/{draft.id}/edit",
            {
                "supplier_id": str(data["suppliers"]["tn"].id),
                "purchase_date": "2026-11-20",
                "gst_type": "none",
            },
        ),
        (
            f"/purchases/{draft.id}/items",
            {
                "item_id": str(data["items"]["oil"].id),
                "entered_qty": "5",
                "unit_rate": "140.00",
            },
        ),
        (
            f"/purchases/{draft.id}/items/{line_id}/edit",
            {"entered_qty": "12", "unit_rate": "42.00"},
        ),
        (f"/purchases/{draft.id}/items/{line_id}/delete", {}),
        (f"/purchases/{draft.id}/post", {}),
        (f"/purchases/{draft.id}/cancel", {"reason": "No token"}),
    ]

    for url, payload in post_cases:
        missing = client.post(url, data=payload)
        assert missing.status_code == 400, f"Missing CSRF did not return 400 for {url}"

        wrong = client.post(url, data={**payload, "csrf_token": "tampered-token"})
        assert wrong.status_code == 400, f"Invalid CSRF did not return 400 for {url}"

    # Verify CSRF is still enforced on POST to edit/add-line for a posted purchase.
    post_purchase(draft.id, actor_user_id=admin.id)
    assert (
        client.post(
            f"/purchases/{draft.id}/edit",
            data={"supplier_id": str(data["suppliers"]["tn"].id)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/purchases/{draft.id}/items",
            data={"item_id": str(data["items"]["oil"].id), "entered_qty": "1", "unit_rate": "10"},
        ).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# 3. Draft Creation, Header Editing, FY Renumbering & Validation Preservation
# ---------------------------------------------------------------------------


def test_draft_create_edit_validation_gst_recalc_and_fy_renumbering(client, db_session):
    _create_user(db_session, username="admin_draft", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    tn = data["suppliers"]["tn"]
    ka = data["suppliers"]["ka"]
    inactive_sup = data["suppliers"]["inactive"]
    flour = data["items"]["flour"]
    pouches = data["items"]["pouches"]
    murukku = data["items"]["murukku"]
    inactive_item = data["items"]["inactive"]

    _login(client, "admin_draft")

    # GET /purchases/new defaults date to business today (IST) and lists active suppliers only
    new_get = client.get("/purchases/new")
    assert new_get.status_code == 200
    assert get_business_today().isoformat().encode() in new_get.data
    assert tn.name.encode() in new_get.data
    assert inactive_sup.name.encode() not in new_get.data

    # Invalid create preserves submitted values and shows inline errors
    bad_create = client.post(
        "/purchases/new",
        data={
            "csrf_token": _csrf(client),
            "supplier_id": "",
            "purchase_date": "not-a-date",
            "gst_type": "invalid_type",
            "supplier_invoice_no": "X" * 85,
            "notes": "Keep this note",
        },
    )
    assert bad_create.status_code == 200
    assert b"Supplier is required." in bad_create.data
    assert b"valid date" in bad_create.data
    assert b"Select a valid GST type." in bad_create.data
    assert b"80 characters or fewer" in bad_create.data
    assert b"Keep this note" in bad_create.data

    # Inactive supplier rejected cleanly via service error
    inactive_post = client.post(
        "/purchases/new",
        data={
            "csrf_token": _csrf(client),
            "supplier_id": str(inactive_sup.id),
            "purchase_date": "2026-03-31",
            "gst_type": "intra_state",
            "supplier_invoice_no": "INV-OLD",
            "notes": "Preserved note",
        },
    )
    assert inactive_post.status_code == 200
    assert b"inactive" in inactive_post.data
    assert b"Preserved note" in inactive_post.data

    # Valid two-step creation: FY 2025-26 draft header
    create_res = client.post(
        "/purchases/new",
        data={
            "csrf_token": _csrf(client),
            "supplier_id": str(tn.id),
            "purchase_date": "2026-03-31",
            "gst_type": "intra_state",
            "supplier_invoice_no": "TN-001",
            "notes": "Morning delivery",
        },
        follow_redirects=True,
    )
    assert create_res.status_code == 200
    assert b"PUR/2025-26/0001" in create_res.data
    assert b"TN-001" in create_res.data
    # All 3 active item types appear in the add-line dropdown; inactive item excluded
    assert flour.name.encode() in create_res.data
    assert pouches.name.encode() in create_res.data
    assert murukku.name.encode() in create_res.data
    assert inactive_item.name.encode() not in create_res.data

    purchase = db_session.execute(db.select(Purchase)).scalar_one()

    # Add a line of Flour (10 kg @ ₹42.00, 5% intra-state -> ₹420 taxable + ₹10.50 CGST + ₹10.50 SGST = ₹441)
    add_res = client.post(
        f"/purchases/{purchase.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(flour.id),
            "entered_qty": "10",
            "entered_unit_id": "",
            "unit_rate": "42.00",
            "gst_rate": "",
        },
        follow_redirects=True,
    )
    assert add_res.status_code == 200
    assert b"420.00" in add_res.data
    assert b"10.50" in add_res.data
    assert b"441.00" in add_res.data

    # Edit draft header: move date to FY 2026-27 and switch GST to inter_state (Karnataka supplier)
    edit_res = client.post(
        f"/purchases/{purchase.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "supplier_id": str(ka.id),
            "purchase_date": "2026-04-02",
            "gst_type": "inter_state",
            "supplier_invoice_no": "KA-2026-9",
            "notes": "Inter-state bill",
        },
        follow_redirects=True,
    )
    assert edit_res.status_code == 200
    assert b"renumbered from PUR/2025-26/0001 to PUR/2026-27/0001" in edit_res.data
    assert b"IGST" in edit_res.data
    assert b"21.00" in edit_res.data  # 5% IGST on ₹420.00
    assert b"441.00" in edit_res.data

    db_session.refresh(purchase)
    assert purchase.purchase_number == "PUR/2026-27/0001"
    assert purchase.gst_type == "inter_state"
    assert purchase.cgst_paise == 0
    assert purchase.sgst_paise == 0
    assert purchase.igst_paise == 2100


# ---------------------------------------------------------------------------
# 4. Draft Line Management: Unit Conversions, Inline Edit, Delete & Errors
# ---------------------------------------------------------------------------


def test_draft_line_add_conversion_inline_edit_remove_and_validation(client, db_session):
    admin = _create_user(db_session, username="admin_lines", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    u = data["units"]
    flour = data["items"]["flour"]
    murukku = data["items"]["murukku"]

    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=admin.id,
        gst_type="intra_state",
    )

    _login(client, "admin_lines")

    # Add Flour using item-specific unit conversion: 2 bag = 50 kg @ ₹1,000/bag -> ₹2,000 taxable, 5% GST -> ₹2,100
    res_bag = client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(flour.id),
            "entered_qty": "2",
            "entered_unit_id": str(u["bag"].id),
            "unit_rate": "1000.00",
            "gst_rate": "",
        },
        follow_redirects=True,
    )
    assert res_bag.status_code == 200
    assert b"2 bag" in res_bag.data
    assert b"50 kg" in res_bag.data
    assert b"2000.00" in res_bag.data
    assert b"50.00" in res_bag.data
    assert b"2100.00" in res_bag.data

    # Add Finished Good (Murukku) using carton -> packet conversion (3 carton = 60 packet) + custom GST override (18%)
    res_fg = client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(murukku.id),
            "entered_qty": "3",
            "entered_unit_id": str(u["carton"].id),
            "unit_rate": "500.00",
            "gst_rate": "18",
        },
        follow_redirects=True,
    )
    assert res_fg.status_code == 200
    assert b"3 carton" in res_fg.data
    assert b"60 packet" in res_fg.data
    assert b"18%" in res_fg.data

    # Validation errors on add-line preserve submitted values (duplicate item, bad qty, incompatible unit)
    dup_res = client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(flour.id),
            "entered_qty": "1",
            "entered_unit_id": str(u["kg"].id),
            "unit_rate": "45.00",
            "gst_rate": "5",
        },
    )
    assert dup_res.status_code == 200
    assert b"already listed" in dup_res.data
    assert b'value="45.00"' in dup_res.data

    incompat_res = client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(data["items"]["pouches"].id),
            "entered_qty": "5",
            "entered_unit_id": str(u["kg"].id),
            "unit_rate": "2.50",
            "gst_rate": "",
        },
    )
    assert incompat_res.status_code == 200
    assert b"incompatible" in incompat_res.data
    assert b'value="2.50"' in incompat_res.data

    bad_decimals = client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(data["items"]["pouches"].id),
            "entered_qty": "1.2345",
            "entered_unit_id": "",
            "unit_rate": "2.555",
            "gst_rate": "150",
        },
    )
    assert bad_decimals.status_code == 200
    assert b"at most 3 decimal places" in bad_decimals.data
    assert b"at most 2 decimal places" in bad_decimals.data
    assert b"must not exceed 100" in bad_decimals.data

    # Inline edit Flour line: switch to metric grams (5000 g = 5 kg @ ₹0.05/g = ₹250.00), override GST to 12%
    db_session.refresh(draft)
    flour_line = next(li for li in draft.items if li.item_id == flour.id)
    murukku_line = next(li for li in draft.items if li.item_id == murukku.id)

    upd_res = client.post(
        f"/purchases/{draft.id}/items/{flour_line.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "entered_qty": "5000",
            "entered_unit_id": str(u["g"].id),
            "unit_rate": "0.05",
            "gst_rate": "12",
        },
        follow_redirects=True,
    )
    assert upd_res.status_code == 200
    assert b"5000 g" in upd_res.data
    assert b"5 kg" in upd_res.data
    assert b"250.00" in upd_res.data
    assert b"12%" in upd_res.data

    # Invalid inline edit flashes danger cleanly without mutating line
    bad_upd = client.post(
        f"/purchases/{draft.id}/items/{flour_line.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "entered_qty": "0",
            "entered_unit_id": str(u["g"].id),
            "unit_rate": "0.05",
            "gst_rate": "12",
        },
        follow_redirects=True,
    )
    assert bad_upd.status_code == 200
    assert b"greater than zero" in bad_upd.data

    # Delete both lines -> totals return to 0.00
    for lid in (flour_line.id, murukku_line.id):
        del_res = client.post(
            f"/purchases/{draft.id}/items/{lid}/delete",
            data={"csrf_token": _csrf(client)},
            follow_redirects=True,
        )
        assert del_res.status_code == 200

    db_session.refresh(draft)
    assert draft.items == []
    assert draft.subtotal_paise == 0
    assert draft.total_paise == 0

    # Non-existent purchase and line return 404
    assert client.get("/purchases/999999").status_code == 404
    assert (
        client.post(
            f"/purchases/{draft.id}/items/999999/delete",
            data={"csrf_token": _csrf(client)},
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 5. Purchase List Filters (Supplier, Status, Date Range & Error Handling)
# ---------------------------------------------------------------------------


def test_purchase_list_filters_and_invalid_range(client, db_session):
    admin = _create_user(db_session, username="admin_filter", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    tn = data["suppliers"]["tn"]
    ka = data["suppliers"]["ka"]
    flour = data["items"]["flour"]

    p_draft = create_draft_purchase(
        supplier_id=tn.id,
        purchase_date=date(2026, 11, 10),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    p_posted = create_draft_purchase(
        supplier_id=ka.id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=admin.id,
        gst_type="inter_state",
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=20_000, unit_rate_paise=4300)],
    )
    p_posted, _ = post_purchase(p_posted.id, actor_user_id=admin.id)

    _login(client, "admin_filter")

    # Unfiltered shows both
    all_res = client.get("/purchases")
    assert all_res.status_code == 200
    assert p_draft.purchase_number.encode() in all_res.data
    assert p_posted.purchase_number.encode() in all_res.data

    # Filter by supplier
    sup_res = client.get(f"/purchases?supplier_id={ka.id}")
    assert p_posted.purchase_number.encode() in sup_res.data
    assert p_draft.purchase_number.encode() not in sup_res.data

    # Filter by status
    st_res = client.get("/purchases?status=draft")
    assert p_draft.purchase_number.encode() in st_res.data
    assert p_posted.purchase_number.encode() not in st_res.data

    # Filter by date range
    dt_res = client.get("/purchases?date_from=2026-11-15&date_to=2026-11-25")
    assert p_posted.purchase_number.encode() in dt_res.data
    assert p_draft.purchase_number.encode() not in dt_res.data

    # Start date > End date renders cleanly with alert (HTTP 200)
    bad_range = client.get("/purchases?date_from=2026-11-25&date_to=2026-11-10")
    assert bad_range.status_code == 200
    assert b"Start date cannot be later than end date." in bad_range.data

    # Invalid date string handled cleanly
    bad_date = client.get("/purchases?date_from=bad-date")
    assert bad_date.status_code == 200
    assert b"valid date" in bad_date.data


# ---------------------------------------------------------------------------
# 6. Posting, Confirmation Modal, Stock Ledger Traceability & Immutability
# ---------------------------------------------------------------------------


def test_post_purchase_ledger_traceability_and_immutability(client, db_session):
    admin = _create_user(db_session, username="admin_post", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    flour = data["items"]["flour"]
    oil = data["items"]["oil"]

    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=admin.id,
        gst_type="intra_state",
        supplier_invoice_no="INV-POST-1",
    )

    _login(client, "admin_post")

    # Empty draft cannot be posted
    empty_post = client.post(
        f"/purchases/{draft.id}/post",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert empty_post.status_code == 200
    assert b"without at least one line item" in empty_post.data
    db_session.refresh(draft)
    assert draft.status == "draft"

    # Add two lines (Flour 50 kg @ ₹42 = ₹2,100 + 5% GST; Oil 20 L @ ₹140 = ₹2,800 + 5% GST)
    client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(flour.id),
            "entered_qty": "50",
            "entered_unit_id": "",
            "unit_rate": "42.00",
            "gst_rate": "",
        },
    )
    client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(oil.id),
            "entered_qty": "20",
            "entered_unit_id": "",
            "unit_rate": "140.00",
            "gst_rate": "",
        },
    )

    # Draft detail includes Bootstrap confirmation modal before posting
    detail_before = client.get(f"/purchases/{draft.id}")
    assert b'id="postPurchaseModal"' in detail_before.data
    assert b"Confirm Purchase Posting" in detail_before.data

    # Post purchase
    post_res = client.post(
        f"/purchases/{draft.id}/post",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert post_res.status_code == 200
    assert b"posted successfully" in post_res.data
    assert b"Posted &amp; Locked" in post_res.data
    # Linked stock ledger section displays both purchase_receipt entries with taxable value only (excluding GST)
    assert b"Purchase Receipt" in post_res.data
    assert b"+50" in post_res.data
    assert b"2100.00" in post_res.data
    assert b"+20" in post_res.data
    assert b"2800.00" in post_res.data

    # Stock ledger page resolves PUR/... reference with clickable link for admin
    ledger_page = client.get("/inventory/ledger")
    assert ledger_page.status_code == 200
    assert draft.purchase_number.encode() in ledger_page.data
    assert f'/purchases/{draft.id}'.encode() in ledger_page.data

    # Immutability guards: editing header, adding/editing/deleting lines, or double posting are all blocked
    db_session.refresh(draft)
    line_id = draft.items[0].id
    assert draft.status == "posted"

    edit_get = client.get(f"/purchases/{draft.id}/edit", follow_redirects=True)
    assert b"cannot be edited because it is posted" in edit_get.data

    edit_post = client.post(
        f"/purchases/{draft.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "supplier_id": str(data["suppliers"]["tn"].id),
            "purchase_date": "2026-11-21",
            "gst_type": "none",
        },
        follow_redirects=True,
    )
    assert b"cannot be edited because it is posted" in edit_post.data

    add_line = client.post(
        f"/purchases/{draft.id}/items",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(data["items"]["pouches"].id),
            "entered_qty": "10",
            "unit_rate": "2.00",
        },
        follow_redirects=True,
    )
    assert b"Cannot add lines" in add_line.data

    upd_line = client.post(
        f"/purchases/{draft.id}/items/{line_id}/edit",
        data={
            "csrf_token": _csrf(client),
            "entered_qty": "99",
            "unit_rate": "50.00",
        },
        follow_redirects=True,
    )
    assert b"Cannot update lines" in upd_line.data

    del_line = client.post(
        f"/purchases/{draft.id}/items/{line_id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert b"Cannot remove lines" in del_line.data

    repost = client.post(
        f"/purchases/{draft.id}/post",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert b"already been posted" in repost.data

    # Ledger entries unchanged (still exactly 2 receipts)
    assert (
        db_session.execute(
            db.select(StockLedger).where(
                StockLedger.source_doc_type == "purchase",
                StockLedger.source_doc_id == draft.id,
            )
        )
        .scalars()
        .all()
        .__len__()
        == 2
    )


# ---------------------------------------------------------------------------
# 7. Draft & Posted Cancellation (Reason Required, Default IST Date, Rollback)
# ---------------------------------------------------------------------------


def test_cancel_draft_and_posted_purchase_with_reason_and_rollback(client, db_session):
    admin = _create_user(db_session, username="admin_cancel", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    flour = data["items"]["flour"]
    oil = data["items"]["oil"]
    today = get_business_today()

    # 1. Draft cancellation
    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=today,
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )

    _login(client, "admin_cancel")

    # Confirm no cancellation_date input is exposed in the UI
    detail_html = client.get(f"/purchases/{draft.id}").data
    assert b'name="cancellation_date"' not in detail_html

    # Missing reason rejected cleanly with inline validation error & open collapse
    no_reason = client.post(
        f"/purchases/{draft.id}/cancel",
        data={"csrf_token": _csrf(client), "reason": "   "},
    )
    assert no_reason.status_code == 200
    assert b"Cancellation reason is required." in no_reason.data
    assert b'id="cancelPurchaseCollapse"' in no_reason.data
    db_session.refresh(draft)
    assert draft.status == "draft"

    # Valid draft cancellation
    cancel_draft_res = client.post(
        f"/purchases/{draft.id}/cancel",
        data={"csrf_token": _csrf(client), "reason": "Duplicate supplier bill"},
        follow_redirects=True,
    )
    assert cancel_draft_res.status_code == 200
    assert b"Cancelled" in cancel_draft_res.data
    assert b"Duplicate supplier bill" in cancel_draft_res.data
    db_session.refresh(draft)
    assert draft.status == "cancelled"
    assert get_item_stock_position(flour.id).qty_milli == 0

    # Cancelled purchase cannot be edited, posted, or cancelled again
    assert b"cannot be edited because it is cancelled" in client.get(
        f"/purchases/{draft.id}/edit", follow_redirects=True
    ).data
    assert b"cannot be posted" in client.post(
        f"/purchases/{draft.id}/post", data={"csrf_token": _csrf(client)}, follow_redirects=True
    ).data
    assert b"already cancelled" in client.post(
        f"/purchases/{draft.id}/cancel",
        data={"csrf_token": _csrf(client), "reason": "Again"},
        follow_redirects=True,
    ).data

    # 2. Posted purchase cancellation (uses default IST business date)
    posted_draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=today,
        actor_user_id=admin.id,
        gst_type="intra_state",
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=25_000, unit_rate_paise=4200)],
    )
    posted, _ = post_purchase(posted_draft.id, actor_user_id=admin.id)
    assert get_item_stock_position(flour.id).qty_milli == 25_000

    cancel_posted_res = client.post(
        f"/purchases/{posted.id}/cancel",
        data={"csrf_token": _csrf(client), "reason": "Returned defective batch"},
        follow_redirects=True,
    )
    assert cancel_posted_res.status_code == 200
    assert b"reversal recorded" in cancel_posted_res.data
    assert b"Reversal Out" in cancel_posted_res.data
    assert b"Returned defective batch" in cancel_posted_res.data
    db_session.refresh(posted)
    assert posted.status == "cancelled"
    assert get_item_stock_position(flour.id).qty_milli == 0
    assert get_item_stock_position(flour.id).value_paise == 0

    # 3. Posted cancellation blocked atomically when stock has already been consumed
    consumed_draft = create_draft_purchase(
        supplier_id=data["suppliers"]["ka"].id,
        purchase_date=today,
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=oil.id, entered_qty_milli=20_000, unit_rate_paise=14_000)],
    )
    posted_oil, _ = post_purchase(consumed_draft.id, actor_user_id=admin.id)

    adj = StockAdjustment(
        adjustment_number="ADJ-CONSUME-1",
        adjustment_type="spoilage_writeoff",
        adjustment_date=today,
        reason="Spilled 5L",
        created_by=admin.id,
    )
    db_session.add(adj)
    db_session.flush()
    post_stock_issue(
        item_id=oil.id,
        movement_date=today,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj.id,
        qty_milli=5_000,
        created_by=admin.id,
    )
    db_session.commit()

    blocked_res = client.post(
        f"/purchases/{posted_oil.id}/cancel",
        data={"csrf_token": _csrf(client), "reason": "Try cancel after consumption"},
        follow_redirects=True,
    )
    assert blocked_res.status_code == 200
    assert b"negative stock" in blocked_res.data
    db_session.refresh(posted_oil)
    assert posted_oil.status == "posted"
    assert get_item_stock_position(oil.id).qty_milli == 15_000

    # Audit log recorded for cancelled purchases
    logs = (
        db_session.execute(
            db.select(AuditLog).where(
                AuditLog.entity_type == "purchase",
                AuditLog.entity_id == posted.id,
                AuditLog.action == "purchase_cancelled",
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 1


# ---------------------------------------------------------------------------
# 8. Inventory Valuation & Purchase Link Visibility Across All Five Roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,can_see_financials",
    [
        (ROLE_ADMIN, True),
        (ROLE_OWNER, True),
        (ROLE_ACCOUNTANT, True),
        (ROLE_PRODUCTION_SUPERVISOR, False),
        (ROLE_SALESMAN, False),
    ],
)
def test_purchase_ledger_rows_respect_valuation_and_link_permissions(
    client, db_session, role, can_see_financials
):
    seed_admin = _create_user(db_session, username="seed_admin_val", role=ROLE_ADMIN)
    _create_user(db_session, username=f"val_actor_{role}", role=role)
    data = _seed_master_data(db_session)
    flour = data["items"]["flour"]

    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=seed_admin.id,
        gst_type="intra_state",
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=40_000, unit_rate_paise=4750)],
    )
    posted, _ = post_purchase(draft.id, actor_user_id=seed_admin.id)

    _login(client, f"val_actor_{role}")

    # /inventory stock summary
    summary = client.get("/inventory")
    assert summary.status_code == 200
    assert b"40 kg" in summary.data
    if can_see_financials:
        assert b"WAC" in summary.data
        assert b"1900.00" in summary.data
        assert b"47.50" in summary.data
    else:
        assert b"WAC" not in summary.data
        assert b"Stock Value" not in summary.data
        assert b"1900.00" not in summary.data
        assert b"47.50" not in summary.data

    # /inventory/ledger shows PUR/... number to everyone, clickable link + costs ONLY to financial roles
    ledger = client.get(f"/inventory/ledger?item_id={flour.id}")
    assert ledger.status_code == 200
    assert b"+40" in ledger.data
    assert posted.purchase_number.encode() in ledger.data
    purchase_href = f'/purchases/{posted.id}'.encode()

    if can_see_financials:
        assert purchase_href in ledger.data
        assert b"Unit Cost" in ledger.data
        assert b"Value Change" in ledger.data
        assert b"1900.00" in ledger.data
        assert b"47.50" in ledger.data
    else:
        assert purchase_href not in ledger.data
        assert b"Unit Cost" not in ledger.data
        assert b"Value Change" not in ledger.data
        assert b"1900.00" not in ledger.data
        assert b"47.50" not in ledger.data


# ---------------------------------------------------------------------------
# 9. Purchase Register & Detail Financial Visibility (CAP_VIEW_FINANCIAL_REPORTS)
# ---------------------------------------------------------------------------


def test_purchase_register_hides_financials_without_financial_report_permission(
    client, db_session, monkeypatch
):
    seed_owner = _create_user(db_session, username="owner_fin_reg", role=ROLE_OWNER)
    _create_user(db_session, username="admin_no_fin_reg", role=ROLE_ADMIN)
    _create_user(db_session, username="acct_fin_reg", role=ROLE_ACCOUNTANT)
    data = _seed_master_data(db_session)
    flour = data["items"]["flour"]
    pouches = data["items"]["pouches"]

    # 1. Verify empty-state colspan matches visible columns when financial reports are disabled.
    monkeypatch.setitem(
        ROLE_PERMISSIONS,
        ROLE_ADMIN,
        ROLE_PERMISSIONS[ROLE_ADMIN] - {CAP_VIEW_FINANCIAL_REPORTS},
    )
    _login(client, "admin_no_fin_reg")
    empty_non_fin = client.get("/purchases")
    assert empty_non_fin.status_code == 200
    assert b'colspan="7"' in empty_non_fin.data
    assert b"Subtotal" not in empty_non_fin.data
    assert b"GST (\xe2\x82\xb9)" not in empty_non_fin.data
    assert b"Total (\xe2\x82\xb9)" not in empty_non_fin.data

    # Seed one intra-state draft (10 kg @ ₹87.50 = ₹875.00 + 5% GST ₹43.76 -> ₹918.76)
    # and one inter-state posted purchase (100 piece @ ₹13.50 = ₹1350.00 + 18% IGST ₹243.00 -> ₹1593.00).
    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 18),
        actor_user_id=seed_owner.id,
        gst_type="intra_state",
        supplier_invoice_no="TN-FIN-01",
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=8750)],
    )
    posted_draft = create_draft_purchase(
        supplier_id=data["suppliers"]["ka"].id,
        purchase_date=date(2026, 11, 19),
        actor_user_id=seed_owner.id,
        gst_type="inter_state",
        supplier_invoice_no="KA-FIN-02",
        lines=[PurchaseLineInput(item_id=pouches.id, entered_qty_milli=100_000, unit_rate_paise=1350)],
    )
    posted, _ = post_purchase(posted_draft.id, actor_user_id=seed_owner.id)

    # 2. User with CAP_RECORD_PURCHASES but WITHOUT CAP_VIEW_FINANCIAL_REPORTS
    res_non_fin = client.get("/purchases")
    assert res_non_fin.status_code == 200
    # Non-financial info and permitted actions remain visible
    assert draft.purchase_number.encode() in res_non_fin.data
    assert posted.purchase_number.encode() in res_non_fin.data
    assert b"2026-11-18" in res_non_fin.data
    assert b"2026-11-19" in res_non_fin.data
    assert data["suppliers"]["tn"].name.encode() in res_non_fin.data
    assert data["suppliers"]["ka"].name.encode() in res_non_fin.data
    assert b"TN-FIN-01" in res_non_fin.data
    assert b"KA-FIN-02" in res_non_fin.data
    assert b"Draft" in res_non_fin.data
    assert b"Posted" in res_non_fin.data
    assert b"+ New Purchase" in res_non_fin.data
    assert f'/purchases/{draft.id}/edit'.encode() in res_non_fin.data
    # Financial column headers and row amounts are absent from rendered HTML
    assert b"Subtotal" not in res_non_fin.data
    assert b"GST (\xe2\x82\xb9)" not in res_non_fin.data
    assert b"Total (\xe2\x82\xb9)" not in res_non_fin.data
    for hidden_amount in (b"875.00", b"43.76", b"918.76", b"1350.00", b"243.00", b"1593.00"):
        assert hidden_amount not in res_non_fin.data

    # 3. Authorized financial user (accountant) sees financial headers, amounts, and colspan="10" on empty filter
    client.post("/logout", data={"csrf_token": _csrf(client)})
    _login(client, "acct_fin_reg")

    res_fin = client.get("/purchases")
    assert res_fin.status_code == 200
    assert b"Subtotal (\xe2\x82\xb9)" in res_fin.data
    assert b"GST (\xe2\x82\xb9)" in res_fin.data
    assert b"Total (\xe2\x82\xb9)" in res_fin.data
    for visible_amount in (b"875.00", b"43.76", b"918.76", b"1350.00", b"243.00", b"1593.00"):
        assert visible_amount in res_fin.data

    empty_fin = client.get("/purchases?status=cancelled")
    assert empty_fin.status_code == 200
    assert b'colspan="10"' in empty_fin.data


def test_purchase_detail_hides_financials_without_financial_report_permission(
    client, db_session, monkeypatch
):
    seed_owner = _create_user(db_session, username="owner_fin_det", role=ROLE_OWNER)
    _create_user(db_session, username="admin_no_fin_det", role=ROLE_ADMIN)
    _create_user(db_session, username="acct_fin_det", role=ROLE_ACCOUNTANT)
    data = _seed_master_data(db_session)
    u = data["units"]
    flour = data["items"]["flour"]
    pouches = data["items"]["pouches"]

    # Intra-state draft using unit conversion: 2 bag (= 50 kg) @ ₹1,087.50/bag = ₹2,175.00
    # 5% intra-state GST -> CGST ₹54.38, SGST ₹54.38, Grand Total ₹2,283.76
    draft = create_draft_purchase(
        supplier_id=data["suppliers"]["tn"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=seed_owner.id,
        gst_type="intra_state",
        supplier_invoice_no="INV-DET-DRAFT",
        lines=[
            PurchaseLineInput(
                item_id=flour.id,
                entered_unit_id=u["bag"].id,
                entered_qty_milli=2_000,
                unit_rate_paise=108_750,
            )
        ],
    )

    # Inter-state posted purchase: 100 piece @ ₹13.50 = ₹1,350.00 + 18% IGST ₹243.00 -> ₹1,593.00
    posted_draft = create_draft_purchase(
        supplier_id=data["suppliers"]["ka"].id,
        purchase_date=date(2026, 11, 20),
        actor_user_id=seed_owner.id,
        gst_type="inter_state",
        supplier_invoice_no="INV-DET-POSTED",
        lines=[
            PurchaseLineInput(
                item_id=pouches.id,
                entered_qty_milli=100_000,
                unit_rate_paise=1350,
            )
        ],
    )
    posted, _ = post_purchase(posted_draft.id, actor_user_id=seed_owner.id)

    # Remove CAP_VIEW_FINANCIAL_REPORTS from ROLE_ADMIN for this test only
    monkeypatch.setitem(
        ROLE_PERMISSIONS,
        ROLE_ADMIN,
        ROLE_PERMISSIONS[ROLE_ADMIN] - {CAP_VIEW_FINANCIAL_REPORTS},
    )
    _login(client, "admin_no_fin_det")

    # 1. Draft detail page for non-financial user
    draft_res = client.get(f"/purchases/{draft.id}")
    assert draft_res.status_code == 200
    # Operational fields stay visible
    assert draft.purchase_number.encode() in draft_res.data
    assert b"Draft" in draft_res.data
    assert flour.name.encode() in draft_res.data
    assert b"2 bag" in draft_res.data
    assert b"50 kg" in draft_res.data
    # All stored financial fields and financial column headers are absent
    for hidden_fragment in (
        b"Taxable (\xe2\x82\xb9)",
        b"CGST (\xe2\x82\xb9)",
        b"SGST (\xe2\x82\xb9)",
        b"Line Total (\xe2\x82\xb9)",
        b"Subtotal",
        b"Grand Total",
        b"Capitalized Stock Value",
        b"Unit Cost (\xe2\x82\xb9)",
        b"Value Change (\xe2\x82\xb9)",
        b"1087.50",
        b"2175.00",
        b"54.38",
        b"2283.76",
        b"GST 5%",
    ):
        assert hidden_fragment not in draft_res.data

    # 2. Posted detail page for non-financial user
    posted_res = client.get(f"/purchases/{posted.id}")
    assert posted_res.status_code == 200
    # Operational fields and stock ledger quantity remain visible
    assert posted.purchase_number.encode() in posted_res.data
    assert b"Posted" in posted_res.data
    assert pouches.name.encode() in posted_res.data
    assert b"100 piece" in posted_res.data
    assert b"+100" in posted_res.data
    assert b"Purchase Receipt" in posted_res.data
    # All financial headers, rates, tax totals, and ledger values are absent
    for hidden_fragment in (
        b"Rate / Unit (\xe2\x82\xb9)",
        b"Taxable (\xe2\x82\xb9)",
        b"GST %",
        b"IGST (\xe2\x82\xb9)",
        b"Line Total (\xe2\x82\xb9)",
        b"Subtotal",
        b"Grand Total",
        b"Unit Cost (\xe2\x82\xb9)",
        b"Value Change (\xe2\x82\xb9)",
        b"13.50",
        b"1350.00",
        b"18%",
        b"243.00",
        b"1593.00",
    ):
        assert hidden_fragment not in posted_res.data

    # 3. Authorized financial user (accountant) sees full financial breakdown on both pages
    client.post("/logout", data={"csrf_token": _csrf(client)})
    _login(client, "acct_fin_det")

    acct_draft = client.get(f"/purchases/{draft.id}")
    assert acct_draft.status_code == 200
    for visible_fragment in (
        b"Rate / Unit (\xe2\x82\xb9)",
        b"Taxable (\xe2\x82\xb9)",
        b"GST %",
        b"CGST (\xe2\x82\xb9)",
        b"SGST (\xe2\x82\xb9)",
        b"Line Total (\xe2\x82\xb9)",
        b"Subtotal (Taxable / Stock Cost)",
        b"Grand Total",
        b"1087.50",
        b"2175.00",
        b"5%",
        b"54.38",
        b"2283.76",
    ):
        assert visible_fragment in acct_draft.data

    acct_posted = client.get(f"/purchases/{posted.id}")
    assert acct_posted.status_code == 200
    for visible_fragment in (
        b"Rate / Unit (\xe2\x82\xb9)",
        b"Taxable (\xe2\x82\xb9)",
        b"GST %",
        b"IGST (\xe2\x82\xb9)",
        b"Line Total (\xe2\x82\xb9)",
        b"Subtotal (Taxable / Stock Cost)",
        b"Grand Total",
        b"Unit Cost (\xe2\x82\xb9)",
        b"Value Change (\xe2\x82\xb9)",
        b"13.50",
        b"1350.00",
        b"18%",
        b"243.00",
        b"1593.00",
    ):
        assert visible_fragment in acct_posted.data
