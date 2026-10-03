from django.urls import path

from . import views

app_name = "ledger"

urlpatterns = [
    path("firm/<int:firm_id>/details/", views.firm_details, name="firm_details"),
    path("dashboard/recent/", views.dashboard_recent, name="dashboard_recent"),
    path("dashboard/review/", views.dashboard_review, name="dashboard_review"),
    path("bills/", views.bills_page, name="bills"),
    path("bill/<int:bill_id>/edit/", views.edit_bill_view, name="edit_bill"),
    path("bill/<int:bill_id>/delete/", views.delete_bill_view, name="delete_bill"),
    path("trash/", views.trash_page, name="trash"),
    path("trash/<int:group_id>/recover/", views.recover_trash, name="recover_trash"),
    path("Ledger/add-transaction/", views.add_transaction, name="add_transaction_cap"),
    path("add-transaction/", views.add_transaction, name="add_transaction"),
    path("add-firm/", views.add_firm, name="add_firm"),
    path("add-representative/", views.add_representative, name="add_representative"),
    path("representatives-for-firm/", views.representatives_for_firm, name="representatives_for_firm"),
    path("history/", views.ledger_history, name="ledger_history"),
    path("firms-for-source-type/", views.firms_for_source_type, name="firms_for_source_type"),
    path(
        "representative/<int:representative_id>/toggle-active/",
        views.toggle_representative_active,
        name="toggle_representative_active",
    ),
    path("dashboard/payments-trend/", views.dashboard_payments_trend, name="dashboard_payments_trend"),
    path("dashboard/business-trend/", views.dashboard_business_trend, name="dashboard_business_trend"),
    path("dashboard/debt-over-time/", views.dashboard_debt_over_time, name="dashboard_debt_over_time"),
    path("dashboard/debt-breakdown/", views.dashboard_debt_breakdown, name="dashboard_debt_breakdown"),
    path("transaction-batch/<int:batch_id>/reverse/", views.reverse_batch, name="reverse_batch"),
    path("transaction-batch/<int:batch_id>/undo/", views.restore_batch, name="restore_batch"),
    path("ajax/add-firm/", views.ajax_add_firm, name="ajax_add_firm"),
    path("ajax/add-representative/", views.ajax_add_representative, name="ajax_add_representative"),
    path("firm/<int:firm_id>/edit/", views.edit_firm, name="edit_firm"),
    path("firm/<int:firm_id>/delete/", views.delete_firm, name="delete_firm"),
    path("firm/<int:firm_id>/archive/", views.delete_firm, name="archive_firm"),
    path("firm/<int:firm_id>/restore/", views.restore_firm, name="restore_firm"),
    path("representative/<int:rep_id>/edit/", views.edit_representative, name="edit_representative"),
    path("representative/<int:rep_id>/delete/", views.delete_representative, name="delete_representative"),
    path("representative/<int:rep_id>/archive/", views.delete_representative, name="archive_representative"),
    path("representative/<int:rep_id>/restore/", views.restore_representative, name="restore_representative"),
]
