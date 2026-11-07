import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

const kBlue = Color(0xFF00B4FF);

void main() => runApp(const App());

class App extends StatelessWidget {
  const App({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'TradingOps',
        debugShowCheckedModeBanner: false,
        theme: ThemeData.dark().copyWith(
          scaffoldBackgroundColor: Colors.black,
          colorScheme: const ColorScheme.dark(primary: kBlue),
          cardColor: const Color(0xFF0B0F14),
        ),
        home: const Gate(),
      );
}

class Api {
  final String base, token;
  Api(this.base, this.token);
  Map<String, String> get _h => {'Authorization': 'Bearer $token', 'Content-Type': 'application/json'};
  Future<Map<String, dynamic>> status() async {
    final r = await http.get(Uri.parse('$base/status'), headers: _h).timeout(const Duration(seconds: 8));
    if (r.statusCode == 401) throw 'unauthorized';
    return jsonDecode(r.body);
  }

  Future<List<dynamic>> events() async {
    final r = await http.get(Uri.parse('$base/events'), headers: _h).timeout(const Duration(seconds: 8));
    return jsonDecode(r.body);
  }

  Future<void> post(String path, bool v) => http
      .post(Uri.parse('$base$path'), headers: _h, body: jsonEncode({'value': v}))
      .timeout(const Duration(seconds: 8));
}

class Gate extends StatefulWidget {
  const Gate({super.key});
  @override
  State<Gate> createState() => _GateState();
}

class _GateState extends State<Gate> {
  Api? api;
  bool loading = true;
  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((p) {
      final b = p.getString('base'), t = p.getString('token');
      setState(() {
        if (b != null && t != null) api = Api(b, t);
        loading = false;
      });
    });
  }

  @override
  Widget build(BuildContext context) {
    if (loading) return const Scaffold(body: Center(child: CircularProgressIndicator()));
    if (api == null) return PairScreen(onPaired: (a) => setState(() => api = a));
    return Dashboard(api: api!, onUnpair: () async {
      final p = await SharedPreferences.getInstance();
      await p.clear();
      setState(() => api = null);
    });
  }
}

class PairScreen extends StatefulWidget {
  final void Function(Api) onPaired;
  const PairScreen({super.key, required this.onPaired});
  @override
  State<PairScreen> createState() => _PairScreenState();
}

class _PairScreenState extends State<PairScreen> {
  final host = TextEditingController(), code = TextEditingController();
  String msg = '';
  bool busy = false;

  Future<void> pair() async {
    setState(() { busy = true; msg = ''; });
    try {
      var h = host.text.trim();
      if (!h.contains(':')) h = '$h:8765';
      final base = 'http://$h';
      final r = await http
          .post(Uri.parse('$base/pair'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'code': code.text.trim(), 'device_name': 'Android phone'}))
          .timeout(const Duration(seconds: 10));
      if (r.statusCode != 200) throw 'Wrong or expired code';
      final token = jsonDecode(r.body)['token'] as String;
      final p = await SharedPreferences.getInstance();
      await p.setString('base', base);
      await p.setString('token', token);
      widget.onPaired(Api(base, token));
    } catch (e) {
      setState(() => msg = 'Could not pair: $e\nCheck Tailscale is on, on both devices.');
    } finally {
      setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        body: SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: ListView(children: [
              const SizedBox(height: 30),
              const Text('TradingOps', style: TextStyle(fontSize: 28, color: kBlue, fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              const Text('On your PC open TradingOps, press "Show pairing code", then enter it here.'),
              const SizedBox(height: 20),
              TextField(controller: host, decoration: const InputDecoration(labelText: 'PC Tailscale address (e.g. 100.64.1.2)')),
              TextField(controller: code, keyboardType: TextInputType.number, decoration: const InputDecoration(labelText: '6-digit code')),
              const SizedBox(height: 16),
              FilledButton(onPressed: busy ? null : pair, child: Text(busy ? 'Pairing…' : 'Pair')),
              const SizedBox(height: 12),
              Text(msg, style: const TextStyle(color: Colors.redAccent)),
            ]),
          ),
        ),
      );
}

class Dashboard extends StatefulWidget {
  final Api api;
  final VoidCallback onUnpair;
  const Dashboard({super.key, required this.api, required this.onUnpair});
  @override
  State<Dashboard> createState() => _DashboardState();
}

class _DashboardState extends State<Dashboard> {
  Map<String, dynamic>? s;
  List<dynamic> ev = [];
  String error = '';
  Timer? t;

  @override
  void initState() {
    super.initState();
    refresh();
    t = Timer.periodic(const Duration(seconds: 10), (_) => refresh());
  }

  @override
  void dispose() { t?.cancel(); super.dispose(); }

  Future<void> refresh() async {
    try {
      final st = await widget.api.status();
      final e = await widget.api.events();
      if (mounted) setState(() { s = st; ev = e; error = ''; });
    } catch (e) {
      if (e == 'unauthorized') { widget.onUnpair(); return; }
      if (mounted) setState(() => error = 'PC not reachable. Is it on, awake, and on Tailscale?');
    }
  }

  Widget row(String k, String v, {bool? good}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
          Text(k, style: const TextStyle(color: Colors.white70)),
          Text(v, style: TextStyle(color: good == null ? Colors.white : (good ? Colors.greenAccent : Colors.redAccent), fontWeight: FontWeight.w600)),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    final st = s;
    return Scaffold(
      appBar: AppBar(title: const Text('TradingOps'), backgroundColor: Colors.black, actions: [
        IconButton(icon: const Icon(Icons.refresh), onPressed: refresh),
        IconButton(icon: const Icon(Icons.link_off), onPressed: widget.onUnpair),
      ]),
      body: RefreshIndicator(
        onRefresh: refresh,
        child: ListView(padding: const EdgeInsets.all(14), children: [
          if (error.isNotEmpty) Card(color: Colors.red.shade900, child: Padding(padding: const EdgeInsets.all(12), child: Text(error))),
          if (st != null) ...[
            Card(child: Padding(padding: const EdgeInsets.all(14), child: Column(children: [
              row('Trading session', (st['session'] as List).isEmpty ? 'none' : (st['session'] as List).join(', ')),
              row('Schedule confirmed', st['schedule_confirmed'] ? 'yes' : 'NO', good: st['schedule_confirmed']),
              row('Internet', st['internet'] == true ? 'connected' : 'problem', good: st['internet'] == true),
              row('Network check', '${st['network']}', good: st['network'] == 'ok'),
              row('Sleep blocked', st['sleep_blocked'] ? 'yes' : 'no'),
              row('Next wake (UTC)', '${st['next_wake_utc'] ?? '-'}'.replaceFirst('T', ' ').split('.').first),
              row('Last healthy check', '${st['last_health_ok'] ?? 'never'}'.replaceFirst('T', ' ').split('.').first),
            ]))),
            Card(child: SwitchListTile(
              title: const Text('Automatic recovery'),
              value: st['recovery_enabled'],
              onChanged: (v) async { await widget.api.post('/control/recovery', v); refresh(); })),
            Card(child: SwitchListTile(
              title: const Text('Suspend new trade entries'),
              subtitle: const Text('Open positions are not touched'),
              value: st['entries_suspended'],
              onChanged: (v) async { await widget.api.post('/control/suspend-entries', v); refresh(); })),
            const Padding(padding: EdgeInsets.only(top: 10, bottom: 4), child: Text('Accounts', style: TextStyle(color: kBlue, fontSize: 16))),
            for (final a in (st['accounts'] as List))
              Card(child: SwitchListTile(
                title: Text('${a['nickname'] ?? a['login']}'),
                subtitle: Text('${a['login']} @ ${a['server']} ${a['is_demo'] == 1 ? '(demo)' : '(LIVE)'}'),
                value: a['enabled'] == 1,
                onChanged: (v) async { await widget.api.post('/control/accounts/${a['id']}/enable', v); refresh(); })),
            const Padding(padding: EdgeInsets.only(top: 10, bottom: 4), child: Text('Recent events', style: TextStyle(color: kBlue, fontSize: 16))),
            for (final e in ev.take(30))
              ListTile(dense: true, title: Text('${e['message']}'), subtitle: Text('${e['kind']}  ${e['ts']}'.split('.').first)),
          ] else if (error.isEmpty) const Center(child: CircularProgressIndicator()),
        ]),
      ),
    );
  }
}
