from decimal import Decimal

from django import forms

from .models import Bill, Firm, Payment, Representative


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

        # New representatives are active by default.
        representative.is_active = True

        if commit:
            representative.save()

        return representative


# Form for adding a bill/payment transaction.
class LedgerTransactionForm(forms.Form):
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
        queryset=Representative.objects.filter(is_deleted=False),
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

        # Ensure payment amount does not exceed remaining debt
        if firm and payment_amount and firm.source_type != Firm.SourceType.LOCAL_MARKET:
            current_debt = firm.current_debt()
            
            # If adding a new bill in the same transaction, the debt will increase
            if adding_new_bill and new_bill_amount:
                current_debt += new_bill_amount
                
            if payment_amount > current_debt:
                raise forms.ValidationError(
                    f"Payment amount cannot be higher than the remaining debt."
                )

        return cleaned_data

    def save(self):
        # Pull final validated values from cleaned_data.
        firm = self.cleaned_data["firm"]
        representative = self.cleaned_data.get("representative")
        bill_choice = self.cleaned_data.get("bill_choice")
        new_bill_number = self.cleaned_data.get("new_bill_number")
        new_bill_amount = self.cleaned_data.get("new_bill_amount")
        payment_amount = self.cleaned_data.get("payment_amount")

        # Capture debt before this transaction.
        previous_debt = firm.current_debt()

        bill = None
        payment = None

        # Local market is paid upfront.
        # So payment amount also becomes the purchase/bill amount.
        if firm.source_type == Firm.SourceType.LOCAL_MARKET and payment_amount:
            bill = Bill.objects.create(
                firm=firm,
                representative=None,
                bill_number="Local Market Purchase",
                bill_amount=payment_amount,
                previous_debt_at_bill_time=previous_debt,
            )

        # For non-local-market sources, create a new bill only when Add New Bill is selected.
        elif bill_choice == "add_new":
            bill = Bill.objects.create(
                firm=firm,
                representative=representative,
                bill_number=new_bill_number,
                bill_amount=new_bill_amount,
                previous_debt_at_bill_time=previous_debt,
            )

        # Create payment and attach it to the local market bill, new bill, or selected existing bill.
        if payment_amount:
            if bill is None and bill_choice:
                bill = Bill.objects.filter(
                    firm=firm,
                    id=bill_choice,
                ).first()

            payment = Payment.objects.create(
                firm=firm,
                representative=representative,
                bill=bill,
                amount=payment_amount,
            )

        # Return summary values for the success message.
        return {
            "firm": firm,
            "bill": bill,
            "payment": payment,
            "previous_debt": previous_debt,
            "remaining_debt": firm.current_debt(),
        }
