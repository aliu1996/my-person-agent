import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:image_picker/image_picker.dart';
import 'package:speech_to_text/speech_to_text.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'push.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await PushClient.bootstrap();
  runApp(MaterialApp(
    title: '24小时个人助理',
    theme: ThemeData(colorSchemeSeed: const Color(0xff5b8cff), brightness: Brightness.dark),
    home: const Assistant(),
  ));
}

class Assistant extends StatefulWidget {
  const Assistant({super.key});
  @override
  State<Assistant> createState() => _AssistantState();
}

class _AssistantState extends State<Assistant> {
  final service = Service();
  late final PushClient push = PushClient(service);
  final phone = TextEditingController();
  final otp = TextEditingController();
  final input = TextEditingController();
  final voice = SpeechToText();
  final tts = FlutterTts();
  final photos = ImagePicker();
  final messages = <JsonMap>[];
  List<JsonMap> rows = [];
  JsonMap? profile;
  XFile? attachment;
  int? session;
  String? turn;
  bool sending = false;
  bool web = false;
  int tab = 0;
  String notice = '';

  Future<void> task(Future<void> Function() action) async {
    if (mounted) setState(() => notice = '');
    try { await action(); } catch (error) {
      if (mounted) setState(() => notice = error.toString());
    }
  }

  Future<void> login() => task(() async {
    final data = await service.request('/auth/loginByPhone', method: 'POST',
      body: {'phone': phone.text.trim(), 'otpCode': otp.text.trim()}) as JsonMap;
    service.saveTokens(data);
    final value = await service.request('/user/profile') as JsonMap;
    if (mounted) setState(() => profile = value);
    await push.register();
  });

  Future<void> send() async {
    final content = input.text.trim();
    if (sending || content.isEmpty) return;
    setState(() => sending = true);
    await task(() async {
      session ??= (await service.request('/chat/session/add', method: 'POST',
        body: {'title': content.substring(0, content.length > 36 ? 36 : content.length)}) as JsonMap)['id'] as int;
      final file = attachment;
      final files = [if (file != null) await service.upload(file, session!)];
      final reply = <String, dynamic>{'role': 'assistant', 'content': '', 'cards': <JsonMap>[]};
      if (!mounted) return;
      setState(() {
        messages.add({'role': 'user', 'content': content});
        messages.add(reply);
        input.clear();
        attachment = null;
      });
      await for (final frame in service.turn({
        'sessionId': session, 'content': content, 'fileIds': files, 'webSearch': web,
      })) {
        if (!mounted) return;
        final data = frame.data;
        if (data['turnId'] != null) turn = data['turnId'].toString();
        setState(() {
          if (frame.event == 'delta') reply['content'] = '${reply['content']}${data['text']}';
          if (frame.event == 'error') {
            if (data['replaceOutput'] == true) reply['content'] = '';
            notice = data['message']?.toString() ?? '生成失败';
          }
          if (frame.event == 'done' && data.containsKey('balanceAfter')) {
            profile?['pointsBalance'] = data['balanceAfter'];
          }
          if (['source', 'artifact', 'memory', 'confirm', 'tool'].contains(frame.event)) {
            (reply['cards'] as List<JsonMap>).add({...data, 'event': frame.event});
          }
        });
      }
    });
    if (mounted) setState(() { sending = false; turn = null; });
  }

  @override
  void initState() {
    super.initState();
    push.onOpenSession = (id) { unawaited(openSession(id)); };
    unawaited(push.prepare());
  }

  Future<void> openSession(int id) => task(() async {
    if (sending) return;
    final history = await service.request('/chat/session/$id/messages') as JsonMap;
    if (mounted) setState(() {
      session = id;
      messages..clear()..addAll((history['items'] as List).cast<JsonMap>());
      tab = 0;
    });
  });

  Future<void> logout() => task(() async {
    await push.unregister();
    service.clearTokens();
    if (mounted) setState(() {
      profile = null;
      session = null;
      messages.clear();
      rows = [];
      tab = 0;
      attachment = null;
      notice = '';
    });
  });

  Future<void> switchTab(int index) => task(() async {
    setState(() { tab = index; rows = []; });
    if (index != 1 && index != 2) return;
    final data = await service.request(index == 1 ? '/library/page' : '/activity/page') as JsonMap;
    if (mounted && tab == index) setState(() => rows = (data['items'] as List).cast<JsonMap>());
  });

  Future<void> openAsset(String id) => task(() async {
    final data = await service.request('/library/$id/preview') as JsonMap;
    await openLink(data['url'].toString());
  });

  Future<void> openLink(String value) async {
    final uri = Uri.tryParse(value);
    if (uri == null || !['http', 'https'].contains(uri.scheme) || !uri.hasAuthority) return;
    await launchUrl(uri, mode: LaunchMode.externalApplication);
  }

  Future<void> camera() => task(() async {
    final image = await photos.pickImage(source: ImageSource.camera, imageQuality: 88);
    if (mounted && image != null) setState(() => attachment = image);
  });

  Future<void> dictate() => task(() async {
    if (voice.isListening) { await voice.stop(); return; }
    if (!await voice.initialize()) throw ApiIssue('语音识别不可用', 400);
    await voice.listen(localeId: 'zh_CN', onResult: (words) {
      if (mounted) input.text = words.recognizedWords;
    });
  });

  Widget card(JsonMap value) {
    switch (value['event']) {
      case 'source':
        return Wrap(children: (value['items'] as List).cast<JsonMap>().map((item) =>
          TextButton(onPressed: () => openLink(item['url'].toString()), child: Text(item['title'].toString()))).toList());
      case 'artifact':
        return ListTile(title: Text(value['title'].toString()), trailing: const Icon(Icons.download),
          onTap: () => openAsset(value['assetId'].toString()));
      case 'confirm':
        return Card(child: ListTile(title: Text(value['title'].toString()), subtitle: Text(value['detail'].toString()),
          trailing: value['decision'] != null ? Text(value['decision'] == 'allow' ? '已允许' : '已拒绝') :
            Wrap(children: ['allow', 'deny'].map((decision) => TextButton(
              child: Text(decision == 'allow' ? '允许' : '拒绝'), onPressed: () => task(() async {
                await service.request('/chat/confirm', method: 'POST',
                  body: {'confirmId': value['confirmId'], 'decision': decision});
                if (mounted) setState(() => value['decision'] = decision);
              }),
            )).toList())));
      case 'memory':
        return ListTile(title: Text(value['undone'] == true ? '已撤销记忆' : '已记住 ${value['content']}'),
          trailing: value['undone'] == true ? null : TextButton(child: const Text('撤销'), onPressed: () => task(() async {
            await service.request('/memory/${value['memoryId']}/undo', method: 'POST');
            if (mounted) setState(() => value['undone'] = true);
          })));
      default:
        return Text('${value['title']} · ${value['status']}');
    }
  }

  Widget chat() => Column(children: [
    Expanded(child: ListView.builder(padding: const EdgeInsets.all(16),
      itemCount: messages.length, itemBuilder: (context, index) {
        final item = messages[index];
        return Card(child: Padding(padding: const EdgeInsets.all(12), child: Column(
          crossAxisAlignment: CrossAxisAlignment.start, children: [
            SelectableText(item['content']?.toString() ?? ''),
            ...(item['cards'] as List<JsonMap>? ?? []).map(card),
            if (item['role'] == 'assistant' && (item['content'] as String).isNotEmpty)
              IconButton(tooltip: '播报', icon: const Icon(Icons.volume_up),
                onPressed: () async { await tts.setLanguage('zh-CN'); await tts.speak(item['content'] as String); }),
          ],
        )));
      })),
    if (attachment != null) InputChip(label: Text(attachment!.name),
      onDeleted: () => setState(() => attachment = null)),
    Padding(padding: const EdgeInsets.symmetric(horizontal: 12),
      child: TextField(controller: input, minLines: 1, maxLines: 4,
        decoration: const InputDecoration(hintText: '输入消息…', border: OutlineInputBorder()))),
    Row(children: [
      IconButton(tooltip: '拍照', onPressed: sending ? null : camera, icon: const Icon(Icons.photo_camera)),
      IconButton(tooltip: '语音输入', onPressed: sending ? null : dictate, icon: const Icon(Icons.mic)),
      FilterChip(label: const Text('联网'), selected: web, onSelected: (value) => setState(() => web = value)),
      const Spacer(),
      if (sending) IconButton(tooltip: '停止', icon: const Icon(Icons.stop), onPressed: session == null ? null :
        () => task(() async { await service.request('/chat/stream/stop', method: 'POST',
          body: turn == null ? {'sessionId': session} : {'turnId': turn}); }))
      else IconButton(tooltip: '发送', icon: const Icon(Icons.send), onPressed: send),
    ]),
  ]);

  Widget loginForm() => ListView(padding: const EdgeInsets.all(24), children: [
    TextField(controller: phone, keyboardType: TextInputType.phone, decoration: const InputDecoration(labelText: '手机号')),
    TextField(controller: otp, keyboardType: TextInputType.number, decoration: const InputDecoration(labelText: '验证码')),
    TextButton(onPressed: () => task(() async {
      await service.request('/auth/sendOtp', method: 'POST', body: {'phone': phone.text.trim()});
      if (mounted) setState(() => notice = '验证码已发送');
    }), child: const Text('发送验证码')),
    FilledButton(onPressed: login, child: const Text('登录')),
  ]);

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('24小时个人助理'), actions: [
      if (profile != null && tab == 0) IconButton(tooltip: '新对话', icon: const Icon(Icons.add),
        onPressed: sending ? null : () => setState(() { session = null; messages.clear(); })),
    ]),
    body: SafeArea(child: Column(children: [
      if (notice.isNotEmpty) Padding(padding: const EdgeInsets.all(8), child: Text(notice, semanticsLabel: '状态 $notice')),
      Expanded(child: profile == null ? loginForm() : switch (tab) {
        0 => chat(),
        1 || 2 => ListView(children: rows.map((row) => ListTile(
          title: Text(row['title'].toString()),
          subtitle: Text((row['body'] ?? row['mimeType'] ?? '').toString()),
          onTap: tab == 1 ? () => openAsset(row['id'].toString()) : () {
            final id = sessionIdFromPayload({'targetUrl': row['targetUrl']});
            if (id != null) unawaited(openSession(id));
          },
        )).toList()),
        _ => ListView(children: [
          ListTile(title: Text((profile?['nickname'] ?? profile?['phone'] ?? '').toString()),
            subtitle: Text('${profile?['pointsBalance'] ?? 0} 积分')),
          SwitchListTile(title: const Text('记忆'), value: profile?['memoryEnabled'] == true,
            onChanged: (value) => task(() async {
              final data = await service.request('/user/profile', method: 'PUT', body: {'memoryEnabled': value}) as JsonMap;
              if (mounted) setState(() => profile = data);
            })),
          ListTile(title: const Text('退出登录'), onTap: logout),
        ]),
      }),
    ])),
    bottomNavigationBar: profile == null ? null : NavigationBar(selectedIndex: tab, onDestinationSelected: switchTab,
      destinations: const [
        NavigationDestination(icon: Icon(Icons.chat_bubble_outline), label: '对话'),
        NavigationDestination(icon: Icon(Icons.folder_outlined), label: '资产库'),
        NavigationDestination(icon: Icon(Icons.notifications_none), label: '动态'),
        NavigationDestination(icon: Icon(Icons.person_outline), label: '我的'),
      ]),
  );

  @override
  void dispose() {
    unawaited(push.dispose());
    service.client.close();
    phone.dispose();
    otp.dispose();
    input.dispose();
    unawaited(voice.cancel());
    unawaited(tts.stop().then<void>((_) {}));
    super.dispose();
  }
}
