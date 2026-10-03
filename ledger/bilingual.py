"""Presentation-only English/Urdu vocabulary. Never translate stored record values."""
import re

from django.utils.html import format_html


_VOCABULARY = """
Corrected chronological balance|درست کردہ تاریخی بقایا
Original posting receipt|اصل اندراج کی رسید
Unavailable for legacy records|پرانے ریکارڈ کے لیے دستیاب نہیں
Total Debt|کُل قرضہ
Remaining Debt|بقایا قرضہ
Previous Debt|پچھلا قرضہ
Debt After Transaction|لین دین کے بعد قرضہ
Debt Change|قرضے میں تبدیلی
Supplier Credits|سپلائر کے پاس زائد رقم
Source Type|خریداری کا ذریعہ
Firm|فرم
Firm Name|فرم کا نام
Representative|نمائندہ
Representative Name|نمائندے کا نام
Phone Number|فون نمبر
Bill|بل
Bill Number|بل نمبر
New Bill Number|نئے بل کا نمبر
Bill Date|بل کی تاریخ
Bill Amount|بل کی رقم
New Bill Amount|نئے بل کی رقم
Payment|ادائیگی
Payment Amount|ادائیگی کی رقم
Other Amount|دوسری رقم
Custom Payment Amount|اپنی مقرر کردہ رقم
Amount|رقم
Date|تاریخ
From Date|آغاز کی تاریخ
To Date|اختتام کی تاریخ
Notes|اضافی معلومات
Status|حیثیت
Actions|اختیارات
Reason|وجہ
Reason for Deletion|حذف کرنے کی وجہ
Reason for Recovery|بحال کرنے کی وجہ
Deleted On|حذف کرنے کا وقت
Transaction Count|لین دین کی تعداد
Opening Debt|ابتدائی قرضہ
Dashboard|کاروباری خلاصہ
Ledger|کھاتہ
Add Transaction|لین دین درج کریں
Recent Transactions|حالیہ لین دین
Transaction History|لین دین کا ریکارڈ
History|لین دین کا ریکارڈ
Firms|فرمیں
Representatives|نمائندے
Bills|بل
Add Bill|بل درج کریں
Saved Bills|درج شدہ بل
Trash|حذف شدہ ریکارڈ
Firms Trash|حذف شدہ فرمیں
Representatives Trash|حذف شدہ نمائندے
Bills Trash|حذف شدہ بل
Transaction Trash|حذف شدہ لین دین
Today's Payments|آج کی ادائیگیاں
This Week's Payments|اس ہفتے کی ادائیگیاں
This Month's Payments|اس ماہ کی ادائیگیاں
Analytics|تجزیہ
Record Review|ریکارڈ کی جانچ
Analytics and Record Review|تجزیہ اور ریکارڈ کی جانچ
Possible Duplicate Bills|ممکنہ دہرے بل
Unusually Large Entries|غیر معمولی بڑی رقوم
Deletion and Recovery Activity|حذف اور بحالی کا ریکارڈ
Median Amount|درمیانی رقم
Comparison Window|تقابلی مدت
Insufficient History|سابقہ ریکارڈ ناکافی ہے
Direct Company|براہِ راست کمپنی
Distributor Company|ڈسٹری بیوٹر کمپنی
Stockist Company|اسٹاکسٹ کمپنی
Open / Local Market|کھلی یا مقامی مارکیٹ
Save|محفوظ کریں
Saved|محفوظ ہو گیا
Already saved|پہلے سے محفوظ ہے
Save Transaction|لین دین محفوظ کریں
Save Bill|بل محفوظ کریں
Save Firm|فرم محفوظ کریں
Save Representative|نمائندہ محفوظ کریں
Edit|ترمیم کریں
Update|تبدیلی محفوظ کریں
Delete|حذف کریں
Recover|بحال کریں
Undo delete|حذف واپس لیں
Cancel|منسوخ کریں
Details|تفصیلات
Apply Filter|فلٹر لگائیں
Clear Filter|فلٹر ہٹائیں
Full History|مکمل ریکارڈ
Previous|پچھلا
Next|اگلا
Show|دکھائیں
Hide|چھپائیں
Active|فعال
Inactive|غیر فعال
Deleted|حذف شدہ
In Trash|حذف شدہ ریکارڈ میں
Firm in Trash|فرم حذف شدہ ریکارڈ میں ہے
Username|صارف نام
Password|پاس ورڈ
Old password|پرانا پاس ورڈ
New password|نیا پاس ورڈ
New password confirmation|نئے پاس ورڈ کی تصدیق
Password confirmation|پاس ورڈ کی تصدیق
Sign In|داخل ہوں
Sign Out|باہر نکلیں
Change Password|پاس ورڈ تبدیل کریں
Update Password|پاس ورڈ تبدیل کریں
Welcome Back|خوش آمدید
Signed out|آپ باہر نکل چکے ہیں
Sign in again|دوبارہ داخل ہوں
Create Account|اکاؤنٹ بنائیں
Return to Dashboard|کاروباری خلاصے پر واپس جائیں
Password Updated Successfully|پاس ورڈ تبدیل ہو گیا
Sign in to manage the ledger|کھاتہ استعمال کرنے کے لیے داخل ہوں
You have been signed out.|آپ اپنے اکاؤنٹ سے باہر نکل چکے ہیں۔
Your password has been changed. You can now continue using the application.|آپ کا پاس ورڈ تبدیل ہو گیا ہے۔ اب آپ ایپ استعمال کر سکتے ہیں۔
Update your password to keep your account secure|اپنے اکاؤنٹ کی حفاظت کے لیے پاس ورڈ تبدیل کریں
Sign up to start managing your ledger|کھاتہ استعمال کرنے کے لیے اکاؤنٹ بنائیں
Already have an account?|کیا آپ کا اکاؤنٹ پہلے سے موجود ہے؟
Firm Debts|فرموں کے قرضے
Edit Firm|فرم میں ترمیم کریں
Edit Representative|نمائندے میں ترمیم کریں
Update Firm|فرم کی تبدیلی محفوظ کریں
Update Representative|نمائندے کی تبدیلی محفوظ کریں
Add Firm|فرم درج کریں
Add New Firm|نئی فرم درج کریں
Add Representative|نمائندہ درج کریں
Add New Representative|نیا نمائندہ درج کریں
Add New Bill|نیا بل درج کریں
Representative Filters|نمائندوں کے فلٹر
All Source Types|خریداری کے تمام ذرائع
All Firms|تمام فرمیں
All transactions|تمام لین دین
Back to firms|فرموں پر واپس جائیں
Back to representatives|نمائندوں پر واپس جائیں
Back to records|ریکارڈ پر واپس جائیں
Back to dashboard|کاروباری خلاصے پر واپس جائیں
Recover firm first|پہلے فرم بحال کریں
Added|اندراج کی تاریخ
Today|آج
This Week|اس ہفتے
This Month|اس ماہ
Week|ہفتہ
Month|ماہ
Year|سال
5 Yrs|پانچ سال
Count|تعداد
Payments|ادائیگیاں
Purchases|خریداری
Payments Over Time|وقت کے لحاظ سے ادائیگیاں
Business Over Time|وقت کے لحاظ سے کاروبار
Debt Overview|قرضے کا خلاصہ
Debt Over Time|وقت کے لحاظ سے قرضہ
Debt by Firm|فرم کے لحاظ سے قرضہ
Period Transactions|منتخب مدت کا لین دین
Correction details|اصلاح کی تفصیلات
Audit details|تبدیلیوں کی تفصیلات
Transaction|لین دین
Record|ریکارڈ
Group|گروہ
Item / Transactions|ریکارڈ / لین دین
Time|وقت
Event|تبدیلی
Transaction reversal|لین دین کا اثر ختم کیا گیا
Transaction restoration|لین دین کا اثر بحال کیا گیا
Bill Created|بل درج کیا گیا
Payment Made|ادائیگی درج کی گئی
Opening Balance|ابتدائی قرضہ
Delete bill and payments|بل اور ادائیگیاں حذف کریں
Delete bill and linked payments?|بل اور متعلقہ ادائیگیاں حذف کریں؟
Recover deletion group?|حذف شدہ پورا گروہ بحال کریں؟
Recover from Trash?|حذف شدہ ریکارڈ بحال کریں؟
Move to Trash?|حذف شدہ ریکارڈ میں منتقل کریں؟
No linked transaction|کوئی متعلقہ لین دین موجود نہیں
No firms added yet.|ابھی کوئی فرم درج نہیں کی گئی۔
No representatives found.|کوئی نمائندہ نہیں ملا۔
No saved bills.|کوئی درج شدہ بل موجود نہیں۔
Firms Trash is empty.|کوئی حذف شدہ فرم موجود نہیں۔
Representatives Trash is empty.|کوئی حذف شدہ نمائندہ موجود نہیں۔
Bills Trash is empty.|کوئی حذف شدہ بل موجود نہیں۔
No deleted transactions.|کوئی حذف شدہ لین دین موجود نہیں۔
No available bills for this firm.|اس فرم کا کوئی دستیاب بل موجود نہیں۔
No ledger history found for this date range.|اس مدت میں کوئی لین دین موجود نہیں۔
No matching bills found.|کوئی ایک جیسے بل نہیں ملے۔
No large entries flagged.|کوئی غیر معمولی بڑی رقم نہیں ملی۔
No recent records to compare.|تقابل کے لیے کوئی حالیہ ریکارڈ موجود نہیں۔
No deletions or recoveries in this period.|اس مدت میں کوئی ریکارڈ حذف یا بحال نہیں کیا گیا۔
Available on the representatives list.|نمائندوں کی فہرست میں دستیاب ہے۔
Available on the Reps page.|نمائندوں کے صفحے پر دستیاب ہے۔
Expand to inspect records and trends|ریکارڈ اور رجحانات دیکھنے کے لیے کھولیں
Open record review|ریکارڈ کی جانچ کھولیں
Select source type|خریداری کا ذریعہ منتخب کریں
Select firm|فرم منتخب کریں
Select representative|نمائندہ منتخب کریں
Select a firm first|پہلے فرم منتخب کریں
No representative — paid in cash|نمائندہ نہیں — نقد ادائیگی
Latest|تازہ ترین
Loading...|معلومات آ رہی ہیں۔۔۔
Saving...|محفوظ ہو رہا ہے۔۔۔
Working...|کارروائی جاری ہے۔۔۔
OFFLINE|انٹرنیٹ دستیاب نہیں
Pending Sync|ہم آہنگی باقی ہے
Transaction saved|لین دین محفوظ ہو گیا
Transaction was already saved|لین دین پہلے سے محفوظ ہے
Firm added|فرم درج ہو گئی
Firm added.|فرم درج ہو گئی۔
Representative added|نمائندہ درج ہو گیا
Representative added.|نمائندہ درج ہو گیا۔
Bill added.|بل درج ہو گیا۔
This bill was already saved.|یہ بل پہلے سے محفوظ ہے۔
Firm recovered from Trash.|فرم بحال ہو گئی۔
Representative recovered from Trash.|نمائندہ بحال ہو گیا۔
Firm moved to Trash. Its balance and transaction history are retained.|فرم حذف شدہ ریکارڈ میں منتقل ہو گئی۔ قرضہ اور لین دین کا ریکارڈ محفوظ ہے۔
Representative moved to Trash. Historical transaction links are retained.|نمائندہ حذف شدہ ریکارڈ میں منتقل ہو گیا۔ سابقہ لین دین کے روابط محفوظ ہیں۔
Choose a source type and firm first. Company bills require an active representative. Local Market bills are paid immediately in cash.|پہلے خریداری کا ذریعہ اور فرم منتخب کریں۔ کمپنی کے بل کے لیے فعال نمائندہ ضروری ہے۔ مقامی مارکیٹ کے بل کی فوری نقد ادائیگی ہوتی ہے۔
Recover deleted records at any time. Transaction recovery restores every transaction in the deletion group.|حذف شدہ ریکارڈ کسی بھی وقت بحال کریں۔ گروہ بحال کرنے سے اس کے تمام لین دین کے اثرات بحال ہوتے ہیں۔
Active bill/payment saves; combined bill and payment count once. Karachi calendar dates, weeks start Monday.|فعال بل اور ادائیگی کے اندراجات؛ ایک ساتھ درج بل اور ادائیگی ایک لین دین شمار ہوتے ہیں۔ کراچی کی تاریخ کے مطابق ہفتہ پیر سے شروع ہوتا ہے۔
Move this entire transaction to Trash and reverse its bill and payment amounts. You can recover it at any time.|پورا لین دین حذف شدہ ریکارڈ میں منتقل کر کے بل اور ادائیگی کے اثرات واپس لیے جائیں گے۔ آپ اسے کسی بھی وقت بحال کر سکتے ہیں۔
Error loading transactions.|لین دین کی معلومات نہیں آ سکیں۔
Could not load bills. Open firm details to retry.|بل نہیں آ سکے۔ دوبارہ کوشش کے لیے فرم کی تفصیلات کھولیں۔
Record review could not load. Open it here to retry.|ریکارڈ کی جانچ نہیں آ سکی۔ دوبارہ کوشش کے لیے یہاں کھولیں۔
Could not load firms. Refresh this page to retry.|فرموں کی معلومات نہیں آ سکیں۔ دوبارہ کوشش کے لیے صفحہ تازہ کریں۔
Could not load representatives. Refresh this page to retry.|نمائندوں کی معلومات نہیں آ سکیں۔ دوبارہ کوشش کے لیے صفحہ تازہ کریں۔
Charts could not load. Check your connection and reopen Analytics to retry.|چارٹ نہیں آ سکے۔ انٹرنیٹ چیک کر کے تجزیہ دوبارہ کھولیں۔
Recent transactions could not refresh. Reload or sign in again to retry.|حالیہ لین دین تازہ نہیں ہو سکا۔ صفحہ تازہ کریں یا دوبارہ داخل ہوں۔
Enter a reason before continuing.|آگے بڑھنے سے پہلے وجہ درج کریں۔
A reason is required.|وجہ درج کرنا ضروری ہے۔
The reason must be 500 characters or fewer.|وجہ پانچ سو یا اس سے کم حروف میں لکھیں۔
Enter a reason of 1 to 500 characters.|وجہ ایک سے پانچ سو حروف میں لکھیں۔
This field is required.|یہ خانہ بھرنا ضروری ہے۔
Enter a number.|رقم درج کریں۔
Enter a valid date.|درست تاریخ درج کریں۔
Select a valid choice. That choice is not one of the available choices.|دستیاب اختیارات میں سے درست انتخاب کریں۔
Select a valid choice. That choice is not one of the available choices.|دستیاب اختیارات میں سے درست انتخاب کریں۔
Selected firm does not belong to the selected source type.|منتخب فرم خریداری کے منتخب ذریعے سے متعلق نہیں ہے۔
Choose a firm belonging to the selected source type.|خریداری کے منتخب ذریعے سے متعلق فرم منتخب کریں۔
The firm does not belong to the selected source type.|فرم خریداری کے منتخب ذریعے سے متعلق نہیں ہے۔
Choose an active representative.|فعال نمائندہ منتخب کریں۔
Choose an active representative belonging to this firm.|اس فرم کا فعال نمائندہ منتخب کریں۔
Choose a representative belonging to this firm.|اس فرم کا نمائندہ منتخب کریں۔
Local Market bills do not use a representative.|مقامی مارکیٹ کے بل میں نمائندہ شامل نہیں ہوتا۔
Local Market does not use representatives.|مقامی مارکیٹ میں نمائندہ شامل نہیں ہوتا۔
Please enter the custom payment amount.|اپنی مقرر کردہ ادائیگی کی رقم درج کریں۔
Please enter the new bill number.|نئے بل کا نمبر درج کریں۔
Please enter the new bill amount.|نئے بل کی رقم درج کریں۔
Choose a bill belonging to the selected firm.|منتخب فرم کا بل منتخب کریں۔
New bill amount is only allowed when Add New Bill is selected.|نئے بل کی رقم کے لیے نیا بل درج کرنے کا اختیار منتخب کریں۔
Enter a payment amount or choose Add New Bill.|ادائیگی کی رقم درج کریں یا نیا بل درج کریں۔
Payment amount cannot be higher than the remaining debt.|ادائیگی کی رقم بقایا قرضے سے زیادہ نہیں ہو سکتی۔
This firm is in Trash and cannot receive new transactions.|یہ فرم حذف شدہ ریکارڈ میں ہے؛ اس میں نیا لین دین درج نہیں ہو سکتا۔
This bill is deleted or does not belong to the selected firm.|یہ بل حذف ہو چکا ہے یا منتخب فرم کا نہیں ہے۔
This request ID was already used for different transaction data.|اس درخواست کا شناختی نمبر دوسرے لین دین کے لیے استعمال ہو چکا ہے۔
There are no active ledger entries to reverse.|اثر واپس لینے کے لیے کوئی فعال اندراج موجود نہیں۔
Transaction batch was not found.|لین دین نہیں ملا۔
Bill was not found or has no creation transaction.|بل یا اس کا اصل لین دین نہیں ملا۔
This Trash item was not found.|حذف شدہ ریکارڈ نہیں ملا۔
This item's deletion state has changed. Refresh Trash and try again.|اس ریکارڈ کی حالت تبدیل ہو چکی ہے۔ حذف شدہ ریکارڈ تازہ کر کے دوبارہ کوشش کریں۔
This transaction has no deletion to undo.|یہ لین دین حذف نہیں ہے۔
You are offline. Saved locally. Will sync when online.|انٹرنیٹ دستیاب نہیں۔ اندراج اس آلے پر محفوظ ہے اور انٹرنیٹ آنے پر بھیجا جائے گا۔
Offline, but sync script not loaded.|انٹرنیٹ اور ہم آہنگی کی سہولت اس وقت دستیاب نہیں۔
Please sign in again. This transaction is queued on this device.|دوبارہ داخل ہوں۔ یہ لین دین اس آلے پر بھیجنے کے لیے محفوظ ہے۔
Connection lost. Transaction queued on this device.|رابطہ ختم ہو گیا۔ لین دین اس آلے پر بھیجنے کے لیے محفوظ ہے۔
Failed to save transaction. The form is still available to retry.|لین دین محفوظ نہیں ہو سکا۔ فارم میں دوبارہ کوشش کر سکتے ہیں۔
Sign in again before syncing queued transactions. They are saved on this device.|محفوظ لین دین بھیجنے سے پہلے دوبارہ داخل ہوں۔ اندراجات اس آلے پر محفوظ ہیں۔
Offline data synced successfully!|محفوظ اندراجات کامیابی سے بھیج دیے گئے۔
Enter your username|اپنا صارف نام درج کریں
Enter your password|اپنا پاس ورڈ درج کریں
Why delete this transaction?|یہ لین دین کیوں حذف کرنا ہے؟
Account Menu|اکاؤنٹ کے اختیارات
Close|بند کریں
Review suggestions only.|یہ صرف جانچ کی تجاویز ہیں۔
Confirm the documents before making changes.|تبدیلی کرنے سے پہلے اصل دستاویزات چیک کریں۔
Same firm, trimmed case-insensitive bill number, date, and amount.|ایک ہی فرم، بل نمبر، تاریخ اور رقم؛ نمبر کے اضافی فاصلے اور انگریزی حروف کا چھوٹا بڑا ہونا نظر انداز کیا جاتا ہے۔
Greater than 3× this firm's median for the same type in the preceding 90 calendar days. Requires 10 preceding records; same-day entries are excluded from the baseline.|اسی فرم اور اندراج کی اسی قسم کی پچھلے نوے دن کی درمیانی رقم سے تین گنا زیادہ۔ کم از کم دس سابقہ اندراجات ضروری ہیں؛ اسی دن کے اندراجات تقابل میں شامل نہیں۔
Duplicate check covers all available bills.|دہرے بلوں کی جانچ تمام دستیاب بلوں پر ہوتی ہے۔
Events count each batch correction, including repeated cycles and members of grouped bill deletions.|ہر لین دین کی اصلاح الگ تبدیلی شمار ہوتی ہے، جس میں بار بار حذف و بحالی اور بل کے ساتھ حذف شدہ متعلقہ لین دین بھی شامل ہیں۔
Change to current debt|موجودہ قرضے میں تبدیلی
Affected transactions|متاثرہ لین دین
Review period|جانچ کی مدت
Insufficient history for recent entries|حالیہ اندراجات کے لیے سابقہ ریکارڈ ناکافی ہے
Distinct transactions affected|متاثرہ الگ لین دین
deletions|حذف
recoveries|بحالی
records|اندراجات
bills|بل
Page|صفحہ
of|از
Pakistani Rupees|پاکستانی روپے
Saving transaction…|لین دین محفوظ ہو رہا ہے۔۔۔
Saving...|محفوظ ہو رہا ہے۔۔۔
Processing...|کارروائی جاری ہے۔۔۔
Please enter a valid positive amount.|درست مثبت رقم درج کریں۔
Please select a firm first.|پہلے فرم منتخب کریں۔
Please select a source type first.|پہلے خریداری کا ذریعہ منتخب کریں۔
Please select a representative.|نمائندہ منتخب کریں۔
"""

VOCABULARY = dict(line.split("|", 1) for line in _VOCABULARY.strip().splitlines())
ALIASES = {
    "Net Balance": "Total Debt", "Net balance · all firms": "Total Debt",
    "Current Net Balance Across All Firms": "Total Debt", "Balance": "Debt",
    "Balance After": "Debt After Transaction", "Balance restored": "Debt Change",
    "Firm Balances": "Firm Debts", "Source": "Source Type", "Phone": "Phone Number",
    "From": "From Date", "To": "To Date", "Apply": "Apply Filter", "Clear": "Clear Filter",
    "Today Payments": "Today's Payments",
    "Add Ledger Transaction": "Add Transaction", "Reason for deleting": "Reason for Deletion",
    "Reason for recovery": "Reason for Recovery", "Median": "Median Amount",
    "Baseline": "Comparison Window", "Deletion and recovery": "Deletion and Recovery Activity",
    "Supplier credit": "Supplier Credits", "Supplier Credit": "Supplier Credits",
    "+ Add Firm": "Add Firm", "+ Add Representative": "Add Representative",
    "Firm moved to Trash. Its balance and transaction history are retained.": "Firm moved to Trash. Its debt and transaction history are retained.",
}
VOCABULARY["Debt"] = "قرضہ"
VOCABULARY.update({
    "Reps": "نمائندے",
    "Help": "رہنمائی",
    "This firm already has active representative(s). Do you want to deactivate the previous representative(s)?": "اس فرم کے فعال نمائندے پہلے سے موجود ہیں۔ کیا آپ پچھلے نمائندوں کو غیر فعال کرنا چاہتے ہیں؟",
    "Could not check existing representatives.": "موجودہ نمائندوں کی جانچ نہیں ہو سکی۔",
    "Could not add firm.": "فرم درج نہیں ہو سکی۔",
    "Could not add representative.": "نمائندہ درج نہیں ہو سکا۔",
    "Monday": "پیر", "Tuesday": "منگل", "Wednesday": "بدھ", "Thursday": "جمعرات", "Friday": "جمعہ", "Saturday": "ہفتہ", "Sunday": "اتوار",
    "Firm moved to Trash. Its debt and transaction history are retained.": "فرم حذف شدہ ریکارڈ میں منتقل ہو گئی۔ قرضہ اور لین دین کا ریکارڈ محفوظ ہے۔",
    "Recovered": "بحال شدہ", "Reversal": "لین دین کا اثر ختم کیا گیا", "Restoration": "لین دین کا اثر بحال کیا گیا", "Adjustment": "حساب کی اصلاح",
    "Bills and Transactions Trash": "حذف شدہ بل اور لین دین", "Trash is empty.": "کوئی حذف شدہ ریکارڈ موجود نہیں۔",
    "Bill Count": "بلوں کی تعداد", "Cash": "نقد", "Transaction Saved": "لین دین محفوظ ہو گیا",
    "Moved to Trash. You can recover this item at any time.": "ریکارڈ حذف شدہ ریکارڈ میں منتقل ہو گیا۔ کسی بھی وقت بحال کر سکتے ہیں۔",
    "Recovered from Trash. The item's financial effect has been restored.": "ریکارڈ بحال ہو گیا اور اس کا مالی اثر دوبارہ شامل ہو گیا ہے۔",
    "Selected representative does not belong to the selected firm.": "منتخب نمائندہ اس فرم کا نہیں ہے۔",
    "Please select an active representative before saving this transaction.": "لین دین محفوظ کرنے سے پہلے فعال نمائندہ منتخب کریں۔",
    "This representative is inactive. Please make them active or add a new representative before saving this transaction.": "یہ نمائندہ غیر فعال ہے۔ لین دین محفوظ کرنے سے پہلے اسے فعال کریں یا نیا نمائندہ درج کریں۔",
    "Local market dealings should not have representatives.": "مقامی مارکیٹ کے لین دین میں نمائندہ شامل نہیں ہونا چاہیے۔",
    "Could not update this transaction. Check your connection or sign in again, then retry.": "لین دین تبدیل نہیں ہو سکا۔ انٹرنیٹ چیک کریں یا دوبارہ داخل ہو کر کوشش کریں۔",
    "Could not load firms": "فرموں کی معلومات نہیں آ سکیں",
    "Could not load representatives": "نمائندوں کی معلومات نہیں آ سکیں",
    "Could not update status": "حیثیت تبدیل نہیں ہو سکی",
    "Please enter a correct username and password. Note that both fields may be case-sensitive.": "درست صارف نام اور پاس ورڈ درج کریں۔ انگریزی حروف کا چھوٹا بڑا ہونا مختلف ہو سکتا ہے۔",
    "Your old password was entered incorrectly. Please enter it again.": "پرانا پاس ورڈ درست نہیں ہے۔ دوبارہ درج کریں۔",
    "The two password fields didn’t match.": "دونوں پاس ورڈ ایک جیسے نہیں ہیں۔",
    "This password is too common.": "یہ پاس ورڈ بہت عام ہے۔",
    "This password is entirely numeric.": "یہ پاس ورڈ صرف ہندسوں پر مشتمل ہے۔",
    "Your password can’t be too similar to your other personal information.": "پاس ورڈ آپ کی دوسری ذاتی معلومات سے بہت ملتا جلتا نہیں ہونا چاہیے۔",
    "Your password can’t be a commonly used password.": "پاس ورڈ بہت عام نہیں ہونا چاہیے۔",
    "Your password can’t be entirely numeric.": "پاس ورڈ صرف ہندسوں پر مشتمل نہیں ہونا چاہیے۔",
    "Enter the same password as before, for verification.": "تصدیق کے لیے وہی پاس ورڈ دوبارہ درج کریں۔",
    "Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.": "ضروری: زیادہ سے زیادہ ایک سو پچاس حروف۔ صرف حروف، ہندسے اور @/./+/-/_ استعمال کریں۔",
    "A user with that username already exists.": "اس صارف نام کا اکاؤنٹ پہلے سے موجود ہے۔",
    "This account is inactive.": "یہ اکاؤنٹ غیر فعال ہے۔",
})
ERROR_PATTERNS = [
    [r"Ensure this value is greater than or equal to (.+)\.", "رقم کم از کم {1} ہونی چاہیے۔"],
    [r"Ensure this value is less than or equal to (.+)\.", "رقم زیادہ سے زیادہ {1} ہونی چاہیے۔"],
    [r"Ensure this value has at most (\d+) characters \(it has (\d+)\)\.", "زیادہ سے زیادہ {1} حروف درج کریں؛ موجودہ تعداد {2} ہے۔"],
    [r"Ensure that there are no more than (\d+) digits in total\.", "کل ہندسے {1} سے زیادہ نہیں ہونے چاہییں۔"],
    [r"Ensure that there are no more than (\d+) decimal places\.", "اعشاریہ کے بعد {1} سے زیادہ ہندسے نہیں ہونے چاہییں۔"],
    [r"Ensure that there are no more than (\d+) digits before the decimal point\.", "اعشاریہ سے پہلے {1} سے زیادہ ہندسے نہیں ہونے چاہییں۔"],
    [r"Your password must contain at least (\d+) characters\.", "پاس ورڈ میں کم از کم {1} حروف ہونے چاہییں۔"],
    [r"This password is too short\. It must contain at least (\d+) characters\.", "پاس ورڈ مختصر ہے۔ کم از کم {1} حروف درج کریں۔"],
    [r"The password is too similar to the (.+)\.", "پاس ورڈ اس معلومات سے بہت ملتا جلتا ہے: {1}۔"],
]
VOCABULARY.update({
    "Edit Bill": "بل میں ترمیم کریں",
    "Update Bill": "بل کی تبدیلی محفوظ کریں",
    "Bill updated.": "بل کی تبدیلی محفوظ ہو گئی۔",
    "This bill was already updated.": "بل کی یہ تبدیلی پہلے ہی محفوظ ہو چکی ہے۔",
    "Bill edits are recorded in history. Amount changes update debt; Local Market cash payments stay equal to the bill.": "بل کی تبدیلیاں ریکارڈ میں محفوظ رہتی ہیں۔ رقم بدلنے سے قرضہ بدلتا ہے؛ مقامی مارکیٹ کی نقد ادائیگی بل کے برابر رہتی ہے۔",
    "Recover this bill from Trash before editing it.": "ترمیم کرنے سے پہلے بل کو حذف شدہ ریکارڈ سے بحال کریں۔",
    "This bill changed after you opened it. Reload and review the latest bill.": "صفحہ کھولنے کے بعد یہ بل تبدیل ہو چکا ہے۔ صفحہ دوبارہ کھول کر تازہ بل دیکھیں۔",
    "Could not update representative status.": "نمائندے کی حیثیت تبدیل نہیں ہو سکی۔",
    "Toggle representative active status": "نمائندے کی فعال حیثیت بدلیں",
})
_CANONICAL = {}
for _key in VOCABULARY:
    _CANONICAL.setdefault(_key.casefold(), _key)
_ALIASES = {key.casefold(): value for key, value in ALIASES.items()}


def wording(value):
    text = str(value)
    english = _ALIASES.get(text.casefold(), _CANONICAL.get(text.casefold(), text))
    urdu = VOCABULARY.get(english, "")
    if not urdu:
        for pattern, translation in ERROR_PATTERNS:
            match = re.fullmatch(pattern, english)
            if match:
                urdu = re.sub(r"\{(\d+)\}", lambda item: "\u2068" + match.group(int(item[1])) + "\u2069", translation)
                break
    return english, urdu


def label(value):
    english, urdu = wording(value)
    if not urdu:
        return format_html('<bdi dir="auto">{}</bdi>', english)
    return format_html(
        '<span class="bi-label"><span lang="en" dir="ltr" class="bi-en">{}</span>'
        '<span lang="ur" dir="rtl" class="bi-ur">{}</span></span>', english, urdu,
    )


def plain(value):
    english, urdu = wording(value)
    return f"{english} — {urdu}" if urdu else english


def catalog():
    return {"labels": {key: list(wording(key)) for key in [*VOCABULARY, *ALIASES]}, "formats": FORMATS, "error_patterns": ERROR_PATTERNS}


FORMATS = {
    "could_not_add_firm": ["Could not add firm: {detail}", "فرم درج نہیں ہو سکی: {detail}"],
    "could_not_add_rep": ["Could not add representative: {detail}", "نمائندہ درج نہیں ہو سکا: {detail}"],
    "receipt": ["{status} · {firm} · Previous Debt {previous} → Remaining Debt {remaining}", "{status_ur} · {firm} · پچھلا قرضہ {previous} ← بقایا قرضہ {remaining}"],
    "transaction_delete": ["Move transaction #{id} for {firm} dated {date} to Trash. This reverses all bill and payment amounts in this transaction. Change to current debt: {amount}. You can recover it from Trash at any time.", "فرم {firm} کا {date} کا لین دین نمبر {id} حذف شدہ ریکارڈ میں منتقل کریں۔ بل اور ادائیگی کے تمام اثرات واپس لیے جائیں گے۔ موجودہ قرضے میں تبدیلی: {amount}۔ کسی بھی وقت بحال کر سکتے ہیں۔"],
    "bill_delete": ["Move bill {bill} for {firm} to Trash. Affected transactions: #{ids} ({count} total). Change to current debt: {amount}. The entire affected group can be recovered together at any time. Previously deleted payments stay deleted. Membership is checked again when you confirm.", "فرم {firm} کا بل {bill} حذف شدہ ریکارڈ میں منتقل کریں۔ متاثرہ لین دین: {ids}؛ کل {count}۔ قرضے میں تبدیلی: {amount}۔ پورا گروہ کسی بھی وقت بحال ہو سکتا ہے۔ پہلے سے حذف شدہ ادائیگیاں بحال نہیں ہوں گی۔ تصدیق کے وقت متعلقہ اندراجات دوبارہ چیک ہوں گے۔"],
    "group_recover": ["Recover {item} for {firm}, including transactions #{ids}. Change to current debt: {amount}. Any earlier independent deletions remain in Trash.", "فرم {firm} کا ریکارڈ {item} اور متعلقہ لین دین {ids} بحال کریں۔ قرضے میں تبدیلی: {amount}۔ پہلے الگ حذف کیے گئے اندراجات حذف شدہ ریکارڈ میں رہیں گے۔"],
    "bill_delete_short": ["Affected transactions: #{ids}. Change to current debt: {amount}. Recover the whole group from Bills Trash at any time.", "متاثرہ لین دین: {ids}۔ قرضے میں تبدیلی: {amount}۔ پورا گروہ حذف شدہ بلوں سے کسی بھی وقت بحال کریں۔"],
    "group_recover_short": ["Recover all transactions #{ids}. Change to debt: {amount}.", "تمام متعلقہ لین دین {ids} بحال کریں۔ قرضے میں تبدیلی: {amount}۔"],
    "review_period": ["Review period: {start}–{end} (Asia/Karachi). Duplicate check covers all available bills.", "جانچ کی مدت: {start} تا {end}؛ کراچی کے وقت کے مطابق۔ دہرے بلوں کی جانچ تمام دستیاب بلوں پر ہوتی ہے۔"],
    "duplicate_count": ["{count} bills matched.", "{count} بل ایک جیسے ہیں۔"],
    "group_count": ["{count} groups", "{count} گروہ"],
    "entry_count": ["{count} entries", "{count} اندراجات"],
    "insufficient": ["Insufficient history for {count} recent entries.", "{count} حالیہ اندراجات کے لیے سابقہ ریکارڈ ناکافی ہے۔"],
    "event_counts": ["{deleted} deletions · {recovered} recoveries", "حذف: {deleted} · بحالی: {recovered}"],
    "affected_count": ["{count} distinct transactions affected.", "{count} الگ لین دین متاثر ہوئے۔"],
    "pagination": ["{count} bills · Page {page} of {pages}", "{count} بل · صفحہ {page} از {pages}"],
    "record_count": ["{count} records", "{count} اندراجات"],
    "pending": ["{count} Pending Sync", "{count} اندراجات بھیجنے باقی ہیں"],
    "not_saved": ["Transaction was not saved: {detail}", "لین دین محفوظ نہیں ہوا: {detail}"],
    "queue_attention": ["Queued transaction needs attention: {detail}", "محفوظ لین دین کی جانچ ضروری ہے: {detail}"],
}


def formatted(key, **values):
    english, urdu = FORMATS[key]
    isolated = {name: "\u2068" + str(value) + "\u2069" for name, value in values.items()}
    return format_html(
        '<span class="bi-label"><span lang="en" dir="ltr" class="bi-en">{}</span>'
        '<span lang="ur" dir="rtl" class="bi-ur">{}</span></span>', english.format(**isolated), urdu.format(**isolated),
    )
