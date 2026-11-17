import 'dart:async';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';

import 'api.dart';

@pragma('vm:entry-point')
Future<void> firebaseBackgroundHandler(RemoteMessage message) async {}

class PushClient {
  PushClient(this.service);
  final Service service;
  String? _token;
  bool available = false;
  void Function(int sessionId)? onOpenSession;
  StreamSubscription<String>? _refresh;
  StreamSubscription<RemoteMessage>? _opened;

  static Future<void> bootstrap() async {
    try {
      if (Firebase.apps.isEmpty) await Firebase.initializeApp();
      FirebaseMessaging.onBackgroundMessage(firebaseBackgroundHandler);
    } catch (_) {}
  }

  Future<void> prepare() async {
    if (Firebase.apps.isEmpty) {
      available = false;
      return;
    }
    try {
      await FirebaseMessaging.instance.requestPermission(alert: true, badge: true, sound: true);
      available = true;
    } catch (_) {
      available = false;
    }
  }

  Future<void> register() async {
    if (!available || service.token == null) return;
    try {
      final status = await service.request('/push/status') as JsonMap;
      if (status['available'] != true) return;
      final token = await FirebaseMessaging.instance.getToken();
      if (token == null || token.length < 20) return;
      final previous = _token;
      await service.registerDevice(
        token, Platform.isIOS ? 'ios' : 'android', previousToken: previous,
      );
      _token = token;
      _refresh ??= FirebaseMessaging.instance.onTokenRefresh.listen((value) {
        unawaited(register());
      });
      _opened ??= FirebaseMessaging.onMessageOpenedApp.listen(_open);
      final initial = await FirebaseMessaging.instance.getInitialMessage();
      if (initial != null) _open(initial);
    } on ApiIssue {
      // Push may be disabled server-side.
    } catch (_) {}
  }

  void _open(RemoteMessage message) {
    final id = sessionIdFromPayload(Map<String, dynamic>.from(message.data));
    if (id != null) onOpenSession?.call(id);
  }

  Future<void> unregister() async {
    final token = _token;
    _token = null;
    if (token == null || service.token == null) return;
    try {
      await service.unregisterDevice(token);
    } catch (_) {}
  }

  Future<void> dispose() async {
    await _refresh?.cancel();
    await _opened?.cancel();
  }
}
