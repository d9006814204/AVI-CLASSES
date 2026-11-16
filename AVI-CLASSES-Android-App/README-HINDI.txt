AVI CLASSES Android App (WebView)
================================

यह Android Studio का प्रोजेक्ट है। ऐप आपकी मौजूदा वेबसाइट
https://avi-classes.onrender.com/ को Android ऐप के अंदर खोलता है।
लॉगिन/सेशन कुकी, JavaScript, Razorpay के वेब पेज और फाइल चुनने की सुविधा के लिए
बुनियादी WebView सेटिंग शामिल हैं।

APK बनाने के चरण:
1. कंप्यूटर में Android Studio (Hedgehog या नया) और Android SDK install करें।
2. Android Studio में इस ZIP से निकला AVI-CLASSES-Android-App फ़ोल्डर खोलें।
3. Gradle Sync पूरा होने दें।
4. Build > Build Bundle(s) / APK(s) > Build APK(s) चुनें।
5. APK app/build/outputs/apk/debug/app-debug.apk में मिलेगा।

ध्यान दें:
- यह प्रोजेक्ट APK बनाने के लिए है; इस वातावरण में Android SDK/Gradle उपलब्ध नहीं था,
  इसलिए APK compile करके परीक्षण नहीं किया गया है।
- ऐप वेबसाइट पर निर्भर है; वेबसाइट/इंटरनेट उपलब्ध होना जरूरी है।
- Play Store पर प्रकाशित करने के लिए अलग signed release build और Play Console प्रक्रिया होगी।
- रिलीज़ से पहले Razorpay payment, login, uploads और अलग-अलग Android फोन पर परीक्षण करें।
