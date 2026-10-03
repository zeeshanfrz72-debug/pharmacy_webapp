from decimal import Decimal
import uuid

from django import forms
from django.db.models import Q

from .models import Bill, Firm, Representative
from django.utils import timezone


BUSINESS_DATE_FORMATS = ["%d-%m-%Y", "%Y-%m-%d"]


# Form for adding a new firm/source.
class FirmForm(forms.ModelForm):
    class Meta:
        model = Firm
        fields = ["name", "source_type", "phone"]


# Form for adding a new representative.
class RepresentativeForm(forms.ModelForm):
    # Helper field used to filter firms; source type is stored on Firm, not Representative.
    source_type = forms.ChoiceField(
        choices=[("", "---------")] + list(Firm.SourceType.choices),
        required=True,
        label="Source Type",
    )

    class Meta:
        model = Representative
        fields = ["source_type", "firm", "name", "phone"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["firm"].queryset = Firm.objects.filter(
            is_deleted=False,
        ).order_by("name")
        if self.instance.pk and self.instance.firm_id:
            self.fields["source_type"].initial = self.instance.firm.source_type

    def clean(self):
        cleaned_data = super().clean()

        source_type = cleaned_data.get("source_type")
        firm = cleaned_data.get("firm")

        # Prevent choosing a firm from a different source type.
        if firm and source_type and firm.source_type != source_type:
            raise forms.ValidationError(
                "Selected firm does not belong to the selected source type."
            )

        return cleaned_data

    def save(self, commit=True):
        # Create the rep object but set extra values before saving.
        representative = super().save(commit=False)

        if not self.instance.pk:
            representative.is_active = True

        if commit:
            representative.save()

        return representative


# Form for adding a bill/payment transaction.
class LedgerTransactionForm(forms.Form):
    posting_rules_version = forms.IntegerField(initial=2, widget=forms.HiddenInput)
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    # Source type filters the firm dropdown.
    source_type = forms.ChoiceField(
        choices=[("", "---------")] + list(Firm.SourceType.choices),
        required=True,
        label="Source Type",
    )

    # Firm is the company/distributor/stockist/local market source.
    firm = forms.ModelChoiceField(
        queryset=Firm.objects.filter(is_deleted=False),
        label="Firm",
    )

    # Representative is required for non-local-market transactions.
    representative = forms.ModelChoiceField(
        queryset=Representative.objects.filter(is_deleted=False, firm__is_deleted=False),
        required=False,
        label="Representative",
    )

    # Hidden/JS-controlled value: either "add_new" or an existing bill id.
    bill_choice = forms.CharField(
        required=False,
        label="Bill Number",
    )

    # New bill number is used only when Add New Bill is selected.
    new_bill_number = forms.CharField(
        max_length=100,
        required=False,
        label="New Bill Number",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )

    # New bill amount is used only when Add New Bill is selected.
    new_bill_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=Decimal("0.00"),
        label="New Bill Amount",
    )

    # Common payment amount choices.
    payment_choice = forms.ChoiceField(
        choices=[
            ("", "No payment"),
            ("1000", "1000 PKR"),
            ("1500", "1500 PKR"),
            ("2000", "2000 PKR"),
            ("2500", "2500 PKR"),
            ("3000", "3000 PKR"),
            ("other", "Other Amount"),
        ],
        required=False,
        initial="",
        label="Payment Amount",
    )

    # Custom amount used when "Other Amount" is selected.
    custom_payment_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=Decimal("0.00"),
        label="Payment Amount",
    )

    def clean(self):
        # Start with Django's built-in cleaned form data.
        cleaned_data = super().clean()

        source_type = cleaned_data.get("source_type")
        firm = cleaned_data.get("firm")
        representative = cleaned_data.get("representative")
        bill_choice = cleaned_data.get("bill_choice")
        new_bill_number = cleaned_data.get("new_bill_number")
        new_bill_amount = cleaned_data.get("new_bill_amount")
        payment_choice = cleaned_data.get("payment_choice")
        custom_payment_amount = cleaned_data.get("custom_payment_amount")
        payment_amount = None

        # Convert payment dropdown/custom field into one final payment amount.
        if payment_choice == "other":
            if not custom_payment_amount:
                raise forms.ValidationError("Please enter the custom payment amount.")

            payment_amount = custom_payment_amount
        elif payment_choice:
            payment_amount = Decimal(payment_choice)

        # Store final payment amount for save().
        cleaned_data["payment_amount"] = payment_amount

        # Prevent choosing a firm from a different source type.
        if firm and source_type and firm.source_type != source_type:
            raise forms.ValidationError(
                "Selected firm does not belong to the selected source type."
            )

        # Prevent choosing a representative from another firm.
        if firm and representative and representative.firm_id != firm.id:
            raise forms.ValidationError(
                "Selected representative does not belong to the selected firm."
            )

        # Non-local-market transactions require an active representative.
        if firm and firm.source_type != Firm.SourceType.LOCAL_MARKET:
            if not representative:
                raise forms.ValidationError(
                    "Please select an active representative before saving this transaction."
                )

            if not representative.is_active:
                raise forms.ValidationError(
                    "This representative is inactive. Please make them active or add a new representative before saving this transaction."
                )

        if representative and not representative.is_active:
            raise forms.ValidationError("Choose an active representative belonging to this firm.")

        adding_new_bill = bill_choice == "add_new"

        if firm and bill_choice and not adding_new_bill:
            if not str(bill_choice).isdigit() or not firm.bills.enabled().filter(pk=bill_choice).exists():
                raise forms.ValidationError("Choose a bill belonging to the selected firm.")

        # Add New Bill requires both bill number and bill amount.
        if adding_new_bill:
            if not new_bill_number and firm and firm.source_type != Firm.SourceType.LOCAL_MARKET:
                raise forms.ValidationError("Please enter the new bill number.")

            if not new_bill_amount:
                raise forms.ValidationError("Please enter the new bill amount.")

        # New bill amount is not allowed unless Add New Bill is selected.
        if not adding_new_bill and new_bill_amount:
            raise forms.ValidationError(
                "New bill amount is only allowed when Add New Bill is selected."
            )

        # User must either add a new bill or enter a payment.
        if not adding_new_bill and not payment_amount:
            raise forms.ValidationError(
                "Enter a payment amount or choose Add New Bill."
            )
        if payment_amount and not bill_choice:
            raise forms.ValidationError("Choose one bill for this payment.")

        return cleaned_data

    def save(self, *, user, payload_hash):
        from .services import create_transaction_batch

        return create_transaction_batch(
            self.cleaned_data,
            user=user,
            payload_hash=payload_hash,
        )


class BillForm(forms.Form):
    posting_rules_version = forms.IntegerField(initial=2, widget=forms.HiddenInput)
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    source_type = forms.ChoiceField(choices=[("", "Select source type")] + list(Firm.SourceType.choices))
    firm = forms.ModelChoiceField(queryset=Firm.objects.filter(is_deleted=False).order_by("name"))
    representative = forms.ModelChoiceField(
        queryset=Representative.objects.filter(is_deleted=False, is_active=True, firm__is_deleted=False).order_by("name"),
        required=False,
    )
    bill_number = forms.CharField(max_length=100, required=False)
    bill_date = forms.DateField(initial=timezone.localdate, input_formats=BUSINESS_DATE_FORMATS, widget=forms.DateInput(attrs={"placeholder": "DD-MM-YYYY", "inputmode": "numeric"}, format="%d-%m-%Y"))
    bill_amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 1}))

    def clean(self):
        data = super().clean()
        firm = data.get("firm")
        rep = data.get("representative")
        if firm and data.get("source_type") != firm.source_type:
            self.add_error("firm", "Choose a firm belonging to the selected source type.")
        if firm:
            if not rep and firm.source_type != Firm.SourceType.LOCAL_MARKET:
                self.add_error("representative", "Choose an active representative.")
            elif rep and rep.firm_id != firm.pk:
                self.add_error("representative", "Choose a representative belonging to this firm.")
            if not data.get("bill_number") and firm.source_type != Firm.SourceType.LOCAL_MARKET:
                self.add_error("bill_number", "A bill number is required for this source type.")
        return data

    def save(self, *, user, payload_hash):
        from .services import create_transaction_batch
        data = self.cleaned_data.copy()
        data.update(
            bill_choice="add_new", new_bill_number=data["bill_number"],
            new_bill_amount=data["bill_amount"],
            payment_amount=None,
        )
        return create_transaction_batch(data, user=user, payload_hash=payload_hash)


class BillEditForm(forms.ModelForm):
    revision = forms.CharField(widget=forms.HiddenInput)
    bill_date = forms.DateField(input_formats=BUSINESS_DATE_FORMATS, widget=forms.DateInput(attrs={"placeholder": "DD-MM-YYYY", "inputmode": "numeric"}, format="%d-%m-%Y"))
    bill_amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = Bill
        fields = ["bill_number", "bill_date", "representative", "bill_amount", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services import bill_revision
        self.fields["revision"].initial = bill_revision(self.instance)
        self.fields["representative"].queryset = Representative.objects.filter(firm_id=self.instance.firm_id).filter(
            Q(is_active=True, is_deleted=False) | Q(pk=self.instance.representative_id)
        ).order_by("name")
        self.fields["representative"].required = self.instance.firm.source_type != Firm.SourceType.LOCAL_MARKET

    def save(self, *, user):
        from .services import edit_bill
        return edit_bill(self.instance.pk, self.cleaned_data, user=user)


class CarryForwardForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    source_revision = forms.CharField(widget=forms.HiddenInput)
    destination_revision = forms.CharField(widget=forms.HiddenInput, required=False)
    destination = forms.ChoiceField(label="Destination bill")
    bill_number = forms.CharField(max_length=100, required=False, label="New Bill Number")
    bill_amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False, label="New charges")
    bill_date = forms.DateField(initial=timezone.localdate, input_formats=BUSINESS_DATE_FORMATS, required=False, widget=forms.DateInput(format="%d-%m-%Y", attrs={"placeholder": "DD-MM-YYYY"}))
    representative = forms.ModelChoiceField(queryset=Representative.objects.none(), required=False)
    notes = forms.CharField(max_length=10000, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    confirm = forms.BooleanField(label="Confirm disable and carry forward")

    def __init__(self, *args, source, **kwargs):
        super().__init__(*args, **kwargs)
        from .services import bill_revision
        self.source = source
        self.destinations = list(source.firm.bills.enabled().exclude(pk=source.pk))
        self.fields["source_revision"].initial = bill_revision(source)
        self.fields["destination"].choices = [("new", "Create a new bill")] + [(str(b.pk), f"{b.display_reference} · {b.remaining_balance}") for b in self.destinations]
        self.fields["destination"].initial = "new"
        self.fields["representative"].queryset = Representative.objects.filter(firm=source.firm, is_active=True, is_deleted=False)

    def clean(self):
        data = super().clean()
        if data.get("destination") == "new":
            for field in ("bill_amount", "bill_date"):
                if not data.get(field):
                    self.add_error(field, "This field is required.")
            if self.source.firm.source_type != Firm.SourceType.LOCAL_MARKET:
                for field in ("bill_number", "representative"):
                    if not data.get(field):
                        self.add_error(field, "This field is required.")
        elif not data.get("destination_revision"):
            self.add_error("destination", "Refresh the destination balance before confirming.")
        return data
