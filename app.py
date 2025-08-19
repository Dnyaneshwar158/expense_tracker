"""
Expense Tracker — Streamlit + SQLite (single‑file project)

Features
- Add/Edit/Delete transactions (expense & income)
- Categories manager
- Monthly budgets per category
- Recurring transactions (auto‑post on/after next date)
- Dashboard with KPIs, monthly trend, category pie, budget progress
- Filters (date, type, category, amount, search)
- CSV import & export
- Local DB backup/restore

How to run
1) Create a virtual env (optional but recommended)
   python -m venv .venv && . .venv/Scripts/activate  # Windows
   # or on macOS/Linux:
   # python3 -m venv .venv && source .venv/bin/activate

2) Install dependencies
   pip install streamlit pandas plotly

3) Save this file as app.py and run
   streamlit run app.py

The app stores data in a local file: expense_tracker.db
"""
from __future__ import annotations
import os
import io
import sqlite3
import datetime as dt
from typing import List, Tuple, Optional

import pandas as pd
import plotly.express as px
import streamlit as st

DB_PATH = "expense_tracker.db"

# ---------- DB Helpers ----------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        PRAGMA journal_mode=WAL;
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,               -- ISO date YYYY-MM-DD
            ttype TEXT NOT NULL CHECK (ttype IN ('Expense','Income')),
            category TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL,
            account TEXT DEFAULT 'Cash',
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT NOT NULL,              -- YYYY-MM
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            UNIQUE(month, category)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recurring (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ttype TEXT NOT NULL CHECK (ttype IN ('Expense','Income')),
            category TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL,
            interval TEXT NOT NULL DEFAULT 'monthly',  -- only 'monthly' supported
            next_date TEXT NOT NULL                    -- YYYY-MM-DD
        );
        """
    )
    # Seed some default categories if empty
    cur.execute("SELECT COUNT(*) AS c FROM categories")
    if cur.fetchone()["c"] == 0:
        cur.executemany(
            "INSERT OR IGNORE INTO categories(name) VALUES(?)",
            [
                ("Food",),
                ("Transport",),
                ("Rent",),
                ("Utilities",),
                ("Shopping",),
                ("Health",),
                ("Entertainment",),
                ("Salary",),
                ("Other",),
            ],
        )
    conn.commit()


# ---------- Utility ----------

def to_date(x: str | dt.date | dt.datetime) -> dt.date:
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return dt.date.fromisoformat(str(x))


def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d: dt.date, months: int) -> dt.date:
    # Simple month arithmetic without external deps
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m-1])
    return dt.date(y, m, day)


# ---------- Recurring processing ----------

def apply_recurring(now: dt.date):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM recurring")
    rows = cur.fetchall()
    made = 0
    for r in rows:
        next_date = to_date(r["next_date"])
        while next_date <= now:
            # Post transaction
            cur.execute(
                """
                INSERT INTO transactions(date, ttype, category, description, amount, account)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    next_date.isoformat(),
                    r["ttype"],
                    r["category"],
                    r["description"],
                    r["amount"],
                    "Recurring",
                ),
            )
            made += 1
            # Advance next_date by interval (monthly only)
            if r["interval"] == "monthly":
                next_date = add_months(next_date, 1)
            else:
                break
        # Update if advanced
        if next_date != to_date(r["next_date"]):
            cur.execute("UPDATE recurring SET next_date=? WHERE id=?", (next_date.isoformat(), r["id"]))
    conn.commit()
    return made


# ---------- Query helpers ----------

def df_transactions(filters: dict | None = None) -> pd.DataFrame:
    conn = get_conn()
    base = "SELECT * FROM transactions"
    where = []
    params: List = []
    if filters:
        if filters.get("date_from"):
            where.append("date >= ?")
            params.append(filters["date_from"].isoformat())
        if filters.get("date_to"):
            where.append("date <= ?")
            params.append(filters["date_to"].isoformat())
        if filters.get("ttype") and filters["ttype"] != "All":
            where.append("ttype = ?")
            params.append(filters["ttype"])
        if filters.get("category") and filters["category"] != "All":
            where.append("category = ?")
            params.append(filters["category"])
        if filters.get("min_amt") is not None:
            where.append("amount >= ?")
            params.append(filters["min_amt"])
        if filters.get("max_amt") is not None:
            where.append("amount <= ?")
            params.append(filters["max_amt"])
        if filters.get("q"):
            where.append("(description LIKE ? OR account LIKE ?)")
            params.extend([f"%{filters['q']}%", f"%{filters['q']}%"])
    if where:
        base += " WHERE " + " AND ".join(where)
    base += " ORDER BY date DESC, id DESC"
    df = pd.read_sql_query(base, conn, params=params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def list_categories() -> List[str]:
    conn = get_conn()
    rows = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
    return [r[0] for r in rows]


def upsert_category(name: str):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
    conn.commit()


def delete_category(name: str):
    conn = get_conn()
    # Also keep existing transactions; category record is just a list
    conn.execute("DELETE FROM categories WHERE name=?", (name,))
    conn.commit()


def set_budget(month: str, category: str, amount: float):
    conn = get_conn()
    conn.execute(
        "INSERT INTO budgets(month, category, amount) VALUES(?,?,?)\n         ON CONFLICT(month, category) DO UPDATE SET amount=excluded.amount",
        (month, category, amount),
    )
    conn.commit()


def get_budgets_df(month: str) -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM budgets WHERE month = ? ORDER BY category",
        conn,
        params=[month],
    )
    return df


# ---------- UI ----------

st.set_page_config(page_title="Expense Tracker", page_icon="💸", layout="wide")
init_db()
posted = apply_recurring(dt.date.today())
if posted:
    st.toast(f"Posted {posted} recurring transaction(s).", icon="✅")

st.title("💸 Expense Tracker")

# Sidebar: quick add & import/export
with st.sidebar:
    st.header("Quick Add")
    with st.form("add_txn", clear_on_submit=True):
        ttype = st.radio("Type", ["Expense", "Income"], horizontal=True)
        today = dt.date.today()
        date = st.date_input("Date", value=today)
        cats = ["+ Add new …"] + list_categories()
        category = st.selectbox("Category", cats)
        if category == "+ Add new …":
            new_cat = st.text_input("New Category Name")
            if new_cat:
                upsert_category(new_cat.strip())
                category = new_cat.strip()
        desc = st.text_input("Description", placeholder="e.g., Groceries at BigBazaar")
        amount = st.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
        account = st.text_input("Account/Wallet", value="Cash")
        submitted = st.form_submit_button("Add Transaction")
        if submitted:
            if category and amount > 0:
                conn = get_conn()
                conn.execute(
                    "INSERT INTO transactions(date, ttype, category, description, amount, account) VALUES(?,?,?,?,?,?)",
                    (date.isoformat(), ttype, category, desc, amount, account),
                )
                conn.commit()
                st.success("Added!")
            else:
                st.error("Please provide category and positive amount.")

    st.divider()
    st.subheader("Import / Export")
    up = st.file_uploader("Import CSV", type=["csv"], accept_multiple_files=False)
    if up is not None:
        try:
            df = pd.read_csv(up)
            required = {"date", "ttype", "category", "description", "amount"}
            if not required.issubset(set(df.columns)):
                st.error(f"CSV must include columns: {sorted(required)}")
            else:
                df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
                df["ttype"] = df["ttype"].where(df["ttype"].isin(["Expense","Income"]), "Expense")
                df["description"] = df["description"].fillna("")
                if "account" not in df:
                    df["account"] = "Imported"
                conn = get_conn()
                conn.executemany(
                    "INSERT INTO transactions(date, ttype, category, description, amount, account) VALUES(?,?,?,?,?,?)",
                    df[["date","ttype","category","description","amount","account"]].itertuples(index=False, name=None),
                )
                conn.commit()
                st.success(f"Imported {len(df)} rows")
        except Exception as e:
            st.exception(e)

    if st.button("Export CSV"):
        df = df_transactions()
        if df.empty:
            st.info("No data to export")
        else:
            csv = df.to_csv(index=False).encode()
            st.download_button(
                "Download transactions.csv",
                data=csv,
                file_name="transactions.csv",
                mime="text/csv",
            )

    st.divider()
    st.subheader("Backup / Restore DB")
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            st.download_button("Download expense_tracker.db", data=f, file_name=DB_PATH)
    restore = st.file_uploader("Restore DB (upload .db)", type=["db"])
    if restore is not None:
        with open(DB_PATH, "wb") as f:
            f.write(restore.read())
        st.success("Database restored. Refresh the app.")

# Filters row
with st.expander("Filters", expanded=False):
    colf1, colf2, colf3, colf4, colf5, colf6 = st.columns([1,1,1,1,1,1])
    with colf1:
        dfrom = st.date_input("From", value=dt.date.today().replace(day=1))
    with colf2:
        dto = st.date_input("To", value=dt.date.today())
    with colf3:
        typ = st.selectbox("Type", ["All", "Expense", "Income"], index=0)
    with colf4:
        cat = st.selectbox("Category", ["All"] + list_categories())
    with colf5:
        min_amt = st.number_input("Min Amount", value=0.0, step=100.0)
    with colf6:
        max_amt = st.number_input("Max Amount", value=0.0, step=100.0, help="0 = ignore")
    q = st.text_input("Search (description/account)")

filters = {
    "date_from": dfrom,
    "date_to": dto,
    "ttype": typ,
    "category": cat,
    "min_amt": None if min_amt == 0 else float(min_amt),
    "max_amt": None if max_amt == 0 else float(max_amt),
    "q": q.strip() or None,
}

# Tabs
TAB_DASH, TAB_TXN, TAB_BUDGETS, TAB_RECUR = st.tabs(["📊 Dashboard", "📒 Transactions", "🎯 Budgets", "🔁 Recurring"])

with TAB_DASH:
    df = df_transactions(filters)
    col1, col2, col3, col4 = st.columns(4)
    total_exp = df.loc[df["ttype"] == "Expense", "amount"].sum() if not df.empty else 0.0
    total_inc = df.loc[df["ttype"] == "Income", "amount"].sum() if not df.empty else 0.0
    balance = total_inc - total_exp
    col1.metric("Expenses", f"₹{total_exp:,.2f}")
    col2.metric("Income", f"₹{total_inc:,.2f}")
    col3.metric("Balance", f"₹{balance:,.2f}")
    col4.metric("Transactions", len(df))

    st.divider()
    if df.empty:
        st.info("No transactions for the selected filters.")
    else:
        # Monthly trend
        dft = df.copy()
        dft["month"] = pd.to_datetime(dft["date"]).dt.to_period("M").astype(str)
        trend = (
            dft.groupby(["month", "ttype"])  # month & type lines
            ["amount"].sum().reset_index()
        )
        fig1 = px.line(trend, x="month", y="amount", color="ttype", markers=True, title="Monthly Trend")
        st.plotly_chart(fig1, use_container_width=True)

        # Category breakdown (expenses only)
        dfe = df[df["ttype"] == "Expense"].copy()
        if not dfe.empty:
            cat_sum = dfe.groupby("category")["amount"].sum().reset_index().sort_values("amount", ascending=False)
            fig2 = px.pie(cat_sum, names="category", values="amount", title="Expenses by Category")
            st.plotly_chart(fig2, use_container_width=True)

        # Budget vs actual (for current month)
        m = month_key(dt.date.today())
        bdf = get_budgets_df(m)
        if not bdf.empty:
            actual = dfe.copy()
            actual["month"] = pd.to_datetime(actual["date"]).dt.to_period("M").astype(str)
            actual_month = actual[actual["month"] == m]
            act = actual_month.groupby("category")["amount"].sum().reset_index()
            merged = bdf.merge(act, on="category", how="left").fillna({"amount_y": 0.0})
            merged.rename(columns={"amount_x": "Budget", "amount_y": "Actual"}, inplace=True)
            fig3 = px.bar(merged, x="category", y=["Budget", "Actual"], barmode="group", title=f"Budget vs Actual — {m}")
            st.plotly_chart(fig3, use_container_width=True)

with TAB_TXN:
    df = df_transactions(filters)
    st.subheader("Transactions")
    if df.empty:
        st.info("No transactions match your filters.")
    else:
        # Inline edit/delete
        for _, row in df.iterrows():
            with st.expander(f"{row['date']} — {row['ttype']} — {row['category']} — ₹{row['amount']:.2f}"):
                c1, c2, c3, c4 = st.columns([1,1,3,1])
                with c1:
                    new_date = st.date_input("Date", to_date(row["date"]))
                    new_type = st.selectbox("Type", ["Expense","Income"], index=0 if row["ttype"]=="Expense" else 1, key=f"type{row['id']}")
                with c2:
                    cats_all = list_categories()
                    new_cat = st.selectbox("Category", cats_all, index=max(0, cats_all.index(row["category"]) if row["category"] in cats_all else 0), key=f"cat{row['id']}")
                    new_amt = st.number_input("Amount", value=float(row["amount"]), step=100.0, key=f"amt{row['id']}")
                with c3:
                    new_desc = st.text_input("Description", value=row["description"], key=f"desc{row['id']}")
                    new_acc = st.text_input("Account", value=row["account"], key=f"acc{row['id']}")
                with c4:
                    if st.button("Save", key=f"save{row['id']}"):
                        conn = get_conn()
                        conn.execute(
                            "UPDATE transactions SET date=?, ttype=?, category=?, description=?, amount=?, account=? WHERE id=?",
                            (new_date.isoformat(), new_type, new_cat, new_desc, float(new_amt), new_acc, int(row["id"]))
                        )
                        conn.commit()
                        st.success("Saved. Reload to see updates above.")
                    if st.button("Delete", key=f"del{row['id']}"):
                        conn = get_conn()
                        conn.execute("DELETE FROM transactions WHERE id=?", (int(row["id"]),))
                        conn.commit()
                        st.warning("Deleted. Reload filters to refresh list.")

with TAB_BUDGETS:
    st.subheader("Monthly Budgets by Category")
    today = dt.date.today()
    months = [month_key(add_months(today.replace(day=1), i)) for i in range(-6, 7)]
    sel_month = st.selectbox("Month", months, index=6)

    # Existing budgets table
    bdf = get_budgets_df(sel_month)
    if bdf.empty:
        st.info("No budgets for this month yet. Add some below.")
    else:
        st.dataframe(bdf, use_container_width=True)

    st.markdown("**Add/Update Budget**")
    c1, c2, c3 = st.columns([2,2,1])
    with c1:
        cat = st.selectbox("Category", list_categories())
    with c2:
        amt = st.number_input("Amount", min_value=0.0, step=500.0)
    with c3:
        if st.button("Save Budget"):
            set_budget(sel_month, cat, float(amt))
            st.success("Budget saved.")

    # Progress vs actual for selected month
    df_all = df_transactions({"date_from": dt.date.fromisoformat(sel_month+"-01"), "date_to": add_months(dt.date.fromisoformat(sel_month+"-01"), 1) - dt.timedelta(days=1), "ttype": "Expense"})
    if not df_all.empty and not bdf.empty:
        act = df_all.groupby("category")["amount"].sum().reset_index()
        merged = bdf.merge(act, on="category", how="left").fillna({"amount_y": 0.0})
        merged.rename(columns={"amount_x": "Budget", "amount_y": "Actual"}, inplace=True)
        merged["Remaining"] = merged["Budget"] - merged["Actual"]
        st.dataframe(merged, use_container_width=True)

with TAB_RECUR:
    st.subheader("Recurring Transactions")
    with st.form("add_recurring", clear_on_submit=True):
        r_type = st.radio("Type", ["Expense","Income"], horizontal=True)
        r_cat = st.selectbox("Category", list_categories())
        r_desc = st.text_input("Description")
        r_amt = st.number_input("Amount", min_value=0.0, step=100.0)
        r_next = st.date_input("Next date", value=dt.date.today())
        submitted = st.form_submit_button("Add Recurring")
        if submitted:
            if r_amt > 0:
                conn = get_conn()
                conn.execute(
                    "INSERT INTO recurring(ttype, category, description, amount, interval, next_date) VALUES(?,?,?,?,?,?)",
                    (r_type, r_cat, r_desc, float(r_amt), "monthly", r_next.isoformat()),
                )
                conn.commit()
                st.success("Recurring item added.")
            else:
                st.error("Amount must be > 0")

    # List & edit recurring
    conn = get_conn()
    r_df = pd.read_sql_query("SELECT * FROM recurring ORDER BY next_date, id", conn)
    if r_df.empty:
        st.info("No recurring items yet.")
    else:
        for _, r in r_df.iterrows():
            with st.expander(f"{r['ttype']} — {r['category']} — ₹{r['amount']:.2f} — next {r['next_date']}"):
                c1, c2, c3, c4 = st.columns([1,1,3,1])
                with c1:
                    nr_type = st.selectbox("Type", ["Expense","Income"], index=0 if r["ttype"]=="Expense" else 1, key=f"rtype{r['id']}")
                    nr_next = st.date_input("Next date", value=to_date(r["next_date"]))
                with c2:
                    nr_cat = st.selectbox("Category", list_categories(), index=max(0, list_categories().index(r["category"]) if r["category"] in list_categories() else 0), key=f"rcat{r['id']}")
                    nr_amt = st.number_input("Amount", value=float(r["amount"]), step=100.0, key=f"ramt{r['id']}")
                with c3:
                    nr_desc = st.text_input("Description", value=r["description"], key=f"rdesc{r['id']}")
                with c4:
                    if st.button("Save", key=f"rsave{r['id']}"):
                        conn.execute(
                            "UPDATE recurring SET ttype=?, category=?, description=?, amount=?, next_date=? WHERE id=?",
                            (nr_type, nr_cat, nr_desc, float(nr_amt), nr_next.isoformat(), int(r["id"]))
                        )
                        conn.commit()
                        st.success("Saved.")
                    if st.button("Delete", key=f"rdel{r['id']}"):
                        conn.execute("DELETE FROM recurring WHERE id=?", (int(r["id"]),))
                        conn.commit()
                        st.warning("Deleted.")

# Categories manager (bottom section)
with st.expander("Manage Categories"):
    st.write("Add or remove category labels. Removing a category does not delete existing transactions; they keep their text value.")
    c1, c2 = st.columns([2,1])
    with c1:
        newc = st.text_input("New category name")
    with c2:
        if st.button("Add Category") and newc.strip():
            upsert_category(newc.strip())
            st.success("Category added.")
    all_cats = list_categories()
    if all_cats:
        delc = st.selectbox("Delete category", ["-"] + all_cats)
        if delc != "-" and st.button("Confirm Delete"):
            delete_category(delc)
            st.warning("Category deleted from list.")

st.caption("Built with ❤️ using Streamlit + SQLite. Save/backup your DB file regularly if this is important data.")
