AVI CLASSES – FINAL STUDENT DASHBOARD DESIGN UPDATE

इस अपडेट में:
1. Student dashboard को Royal Blue + White + Yellow थीम में नया डिज़ाइन दिया गया है।
2. Dashboard में My Courses, Test Series और Courses अलग-अलग रखे गए हैं।
3. My Courses में केवल उस विद्यार्थी के enrolled/purchased course items दिखाई देंगे।
4. Test Series के लिए अलग catalog page बनाया गया है। Mock Test, Bihar Police, BPSC, BSSC, TRE Teacher और STET/BTET अलग-अलग दिखेंगे।
5. हर Test Series के detail page पर पहले से मौजूद subject-wise question count और 150-question target दिखेगा।
6. Mobile layout के लिए responsive styling जोड़ी गई है।
7. Existing database tables, DATABASE_URL और Render environment variables को नहीं बदला गया है।

महत्वपूर्ण भुगतान नोट:
इस ZIP में visual/catalog और navigation अपडेट है। मौजूदा app.py में /payment route शुरुआत में ही student dashboard पर redirect कर रहा है, इसलिए वास्तविक Razorpay checkout अभी सक्रिय नहीं है। मौजूदा Razorpay setup 6-महीने की membership subscription के लिए है; ₹29 test series और अलग-अलग ₹199/₹299 courses के one-time payments के लिए अलग Razorpay Order + payment verification + per-item purchase record बनाना होगा। इसलिए Buy Now/Payment को झूठा सक्रिय नहीं दिखाया गया है। जब तक यह अलग payment workflow नहीं बनेगा, इन कार्डों के Details/View लिंक केवल संबंधित catalog या content तक ले जाएंगे।

Deploy:
- ZIP extract करें।
- GitHub project में app.py, static/style.css, templates/student.html, templates/base.html, templates/my_courses.html, templates/test_series_catalog.html को उनके समान paths पर replace/add करें।
- Existing DATABASE_URL और बाकी Render environment variables न बदलें।
- GitHub commit/push के बाद Render deploy करें।

Syntax check: app.py py_compile सफल। Live Render/Razorpay testing इस environment से नहीं की गई है।
