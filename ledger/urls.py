from django.urls import path

from . import views

app_name = "ledger"

urlpatterns = [
    # Redirect capitalized path to lower-case view for compatibility
    path('Ledger/add-transaction/', views.add_transaction, name='add_transaction_cap'),
    # Main page where dad enters bills/payments.
    path("add-transaction/", views.add_transaction, name="add_transaction"),

    # Page for adding a new firm/company/distributor/stockist/local seller.
    path("add-firm/", views.add_firm, name="add_firm"),

    # Page for adding a new representative/salesman/collector.
    path("add-representative/", views.add_representative, name="add_representative"),

    # Small JSON endpoint used by JavaScript.
    # When firm is selected, JavaScript calls this URL and asks:
    # "Which representatives belong to this firm?"
    path(
        "representatives-for-firm/",
        views.representatives_for_firm,
        name="representatives_for_firm",
    ),
     path("history/", views.ledger_history, name="ledger_history"),
    path(
        "firms-for-source-type/",
        views.firms_for_source_type,
        name="firms_for_source_type",
    ),
    path(
    "representative/<int:representative_id>/toggle-active/",
    views.toggle_representative_active,
    name="toggle_representative_active",
),
path(
    "representative/<int:representative_id>/toggle-active/",
    views.toggle_representative_active,
    name="toggle_representative_active",
),
path("dashboard/payments-trend/", views.dashboard_payments_trend, name="dashboard_payments_trend"),
path("dashboard/business-trend/", views.dashboard_business_trend, name="dashboard_business_trend"),
path("dashboard/debt-over-time/", views.dashboard_debt_over_time, name="dashboard_debt_over_time"),
path("dashboard/debt-breakdown/", views.dashboard_debt_breakdown, name="dashboard_debt_breakdown"),
 path('ledger-entry/<int:entry_id>/soft-delete/', views.soft_delete_ledger_entry, name='soft_delete_ledger_entry'),
 path("ajax/add-firm/", views.ajax_add_firm, name="ajax_add_firm"),
 path("ajax/add-representative/", views.ajax_add_representative, name="ajax_add_representative"),
 path("firm/<int:firm_id>/edit/", views.edit_firm, name="edit_firm"),
 path("firm/<int:firm_id>/delete/", views.soft_delete_firm, name="soft_delete_firm"),
 path("representative/<int:rep_id>/edit/", views.edit_representative, name="edit_representative"),
 path("representative/<int:rep_id>/delete/", views.soft_delete_representative, name="soft_delete_representative"),
]