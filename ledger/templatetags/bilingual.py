from django import template
from django.utils.html import format_html, format_html_join, json_script, strip_tags

from ledger.bilingual import catalog, formatted, label, plain, wording

register = template.Library()


@register.simple_tag
def bi(text):
    return label(text)


@register.simple_tag
def table_heading(text):
    english, urdu = wording(text)
    compact = {
        "Representative": ("Rep", "نمائندہ"), "Representatives": ("Reps", "نمائندے"),
        "Payment Amount": ("Paid", "ادائیگی"), "Payments": ("Paid", "ادائیگی"),
        "Phone Number": ("Phone", "فون"), "Source Type": ("Source", "ذریعہ"),
        "Actions": ("More", "مزید"), "Bill Amount": ("Bill Rs.", "بل کی رقم"),
        "Item / Transactions": ("Record", "ریکارڈ"), "Deleted On": ("Deleted", "وقتِ حذف"),
        "Debt Change": ("Change", "تبدیلی"), "Comparison Window": ("Window", "مدت"),
        "Median Amount": ("Median", "درمیانی رقم"), "Transaction Count": ("Count", "تعداد"),
    }
    short_en, short_ur = compact.get(english, (english, urdu))
    return format_html('<span class="bi-label table-label" title="{}" aria-label="{}">'
                       '<span lang="en" dir="ltr" class="bi-en" aria-hidden="true">{}</span>'
                       '<span lang="ur" dir="rtl" class="bi-ur" aria-hidden="true">{}</span></span>',
                       plain(text), plain(text), short_en, short_ur)


@register.simple_tag
def bi_format(key, **values):
    return formatted(key, **values)


@register.simple_tag
def bi_format_urdu(key, **values):
    from ledger.bilingual import FORMATS
    return FORMATS[key][1].format(**{name: "\u2068" + str(value) + "\u2069" for name, value in values.items()})


register.filter("bilingual", label)
register.filter("bilingual_plain", plain)


@register.filter
def bilingual_record_title(value):
    # Only the system-generated prefix is translated; record references stay exact.
    for prefix in ("Transaction #", "Bill "):
        if str(value).startswith(prefix):
            return format_html('{} <bdi dir="auto">{}</bdi>', label(prefix.strip().rstrip(" #")), str(value)[len(prefix.rstrip("#")):])
    return format_html('<bdi dir="auto">{}</bdi>', value)


@register.simple_tag
def bilingual_catalog():
    return json_script(catalog(), "bilingual-catalog")


@register.simple_tag
def field_label(field):
    names = {"phone": "Phone Number", "name": "Representative Name" if "Representative" in type(field.form).__name__ else "Firm Name"}
    text = names.get(field.name, field.label)
    return format_html('<label for="{}">{}</label>', field.id_for_label, label(text))


@register.filter
def bilingual_errors(errors):
    if not errors:
        return ""
    if hasattr(errors, "values"):
        errors = [message for messages in errors.values() for message in messages]
    return format_html('<ul class="errorlist">{}</ul>', format_html_join("", '<li>{}</li>', ((label(error),) for error in errors)))


@register.filter
def bilingual_help(text):
    # Django auth help may include lists. Translate each trusted sentence and
    # escape it rather than treating arbitrary help text as executable markup.
    items = [strip_tags(item).strip() for item in str(text).split('</li>')]
    return format_html_join(' ', '<span class="helptext">{}</span>', ((label(item),) for item in items if item))


@register.simple_tag
def bilingual_select(field):
    # Only fixed choices are translated. Database-backed names remain verbatim.
    from django.forms import ModelChoiceField
    if not isinstance(field.field, ModelChoiceField) and getattr(field.field, "choices", None):
        field.field.choices = [(value, plain(text)) for value, text in field.field.choices]
    return field.as_widget()
