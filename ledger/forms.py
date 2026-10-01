from decimal import Decimal
import uuid

from django import forms

from .models import Firm, Representative


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
        ).exclude(source_type=Firm.SourceType.LOCAL_MARKET).order_by("name")
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
            ("1000", "1000 PKR"),
            ("1500", "1500 PKR"),
            ("2000", "2000 PKR"),
            ("2500", "2500 PKR"),
            ("3000", "3000 PKR"),
            ("other", "Other Amount"),
        ],
        required=False,
        initial="1000",
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

        # Local market should not use reps or bill numbers.
        if firm and firm.source_type == Firm.SourceType.LOCAL_MARKET:
            cleaned_data["bill_choice"] = ""
            cleaned_data["new_bill_number"] = ""

            if representative:
                raise forms.ValidationError(
                    "Local market dealings should not have representatives."
                )

        adding_new_bill = bill_choice == "add_new"

        if firm and bill_choice and not adding_new_bill:
            if not firm.bills.filter(pk=bill_choice).exists():
                raise forms.ValidationError("Choose a bill belonging to the selected firm.")

        # Add New Bill requires both bill number and bill amount.
        if adding_new_bill:
            if not new_bill_number:
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

        return cleaned_data

    def save(self, *, user, payload_hash):
        from .services import create_transaction_batch

        return create_transaction_batch(
            self.cleaned_data,
            user=user,
            payload_hash=payload_hash,
        )
