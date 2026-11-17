import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';

typedef JsonMap = Map<String, dynamic>;

class ApiIssue implements Exception {
  final String message;
  final int status;
  ApiIssue(this.message, this.status);
  @override
  String toString() => message;
}

class Service {
  final http.Client client = http.Client();
  final String apiUrl = const String.fromEnvironment(
    'CHAT_API_URL', defaultValue: 'http://10.0.2.2:8100',
  );
  final String streamUrl = const String.fromEnvironment(
    'CHAT_STREAM_URL', defaultValue: 'http://10.0.2.2:8101',
  );
  String? token;
  String? refresh;

  Future<dynamic> request(String path, {String method = 'GET', JsonMap? body}) async {
    try {
      return await _request(path, method, body);
    } on ApiIssue catch (error) {
      if (error.status != 401 || refresh == null || path.startsWith('/auth/')) rethrow;
      saveTokens(await _request('/auth/refresh', 'POST', {'refreshToken': refresh}) as JsonMap);
      return _request(path, method, body);
    }
  }

  Future<dynamic> _request(String path, String method, JsonMap? body) async {
    final base = path.startsWith('/chat/stream/') ? streamUrl : apiUrl;
    final request = http.Request(method, Uri.parse('$base/api$path'));
    if (token != null) request.headers['Authorization'] = 'Bearer $token';
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    final response = await http.Response.fromStream(await client.send(request).timeout(const Duration(seconds: 15)));
    final result = jsonDecode(utf8.decode(response.bodyBytes)) as JsonMap;
    if (response.statusCode >= 400 || result['code'] != 0) {
      throw ApiIssue(result['message']?.toString() ?? '请求失败', response.statusCode);
    }
    return result['data'];
  }

  void saveTokens(JsonMap credentials) {
    token = credentials['userJwt'] as String;
    refresh = credentials['refreshToken'] as String;
  }

  Future<int> upload(XFile file, int session) async {
    final bytes = await file.readAsBytes();
    final mime = file.mimeType ?? (file.name.toLowerCase().endsWith('.png') ? 'image/png' : 'image/jpeg');
    final signed = await request('/file/presignUpload', method: 'POST', body: {
      'fileName': file.name, 'mimeType': mime, 'fileSize': bytes.length, 'sessionId': session,
    }) as JsonMap;
    final headers = Map<String, String>.from(signed['headers'] as Map);
    final response = await client.put(Uri.parse(signed['uploadUrl'] as String), headers: headers, body: bytes);
    if (response.statusCode >= 400) throw ApiIssue('图片上传失败', response.statusCode);
    final id = signed['fileId'] as int;
    await request('/file/confirmUpload', method: 'POST', body: {'fileId': id});
    return id;
  }

  Stream<({String event, JsonMap data})> turn(JsonMap body) async* {
    var lastId = '';
    final ids = <String>{};
    for (var attempt = 0; attempt < 4; attempt++) {
      final uri = Uri.parse('$streamUrl/api/chat/stream/${attempt == 0 ? 'send' : 'resume'}');
      final request = http.Request(
        attempt == 0 ? 'POST' : 'GET',
        attempt == 0 ? uri : uri.replace(queryParameters: {
          'sessionId': body['sessionId'].toString(), if (lastId.isNotEmpty) 'lastEventId': lastId,
        }),
      );
      request.headers['Authorization'] = 'Bearer $token';
      if (attempt == 0) {
        request.headers['Content-Type'] = 'application/json';
        request.body = jsonEncode(body);
      }
      try {
        final response = await client.send(request).timeout(const Duration(seconds: 20));
        if (response.statusCode != 200) {
          await response.stream.drain<void>();
          throw ApiIssue('流式连接失败', response.statusCode);
        }
        var event = 'message';
        var id = '';
        final data = <String>[];
        await for (final line in response.stream
            .timeout(const Duration(seconds: 35)).transform(utf8.decoder).transform(const LineSplitter())) {
          if (line.startsWith('event:')) event = line.substring(6).trim();
          if (line.startsWith('id:')) id = line.substring(3).trim();
          if (line.startsWith('data:')) data.add(line.substring(5).trimLeft());
          if (line.isNotEmpty) continue;
          if (data.isNotEmpty && (id.isEmpty || ids.add(id))) {
            if (id.isNotEmpty) lastId = id;
            yield (event: event, data: jsonDecode(data.join('\n')) as JsonMap);
            if (event == 'done' || event == 'error') return;
          }
          event = 'message';
          id = '';
          data.clear();
        }
      } on ApiIssue catch (error) {
        if (error.status < 500) rethrow;
      } on http.ClientException {
        // Backfill after the last complete frame.
      } on TimeoutException {
        // A heartbeat timeout resumes the same turn.
      }
      await Future<void>.delayed(Duration(milliseconds: (attempt + 1) * 350));
    }
    throw ApiIssue('连接中断，请重新打开会话查看结果', 503);
  }
}
