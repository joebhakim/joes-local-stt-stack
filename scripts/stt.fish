#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))

set -l command "$argv[1]"
set -e argv[1]

switch "$command"
    case start
        fish "$script_dir/start_all.fish" $argv
    case stop
        fish "$script_dir/stop_all.fish" $argv
    case restart
        fish "$script_dir/restart_all.fish" $argv
    case status
        fish "$script_dir/status_all.fish" $argv
    case logs
        fish "$script_dir/logs.fish" $argv
    case android-pin android-token
        fish "$script_dir/android_bridge_token.fish" $argv
    case android-reverse
        fish "$script_dir/android_adb_reverse.fish" $argv
    case -h --help ''
        echo "Usage: fish scripts/stt.fish <start|stop|restart|status|logs|android-pin|android-token|android-reverse> [args]"
        echo
        echo "Examples:"
        echo "  fish scripts/stt.fish status"
        echo "  fish scripts/stt.fish restart --force"
        echo "  fish scripts/stt.fish logs 80"
        echo "  fish scripts/stt.fish android-pin"
        echo "  fish scripts/stt.fish android-reverse"
    case '*'
        echo "error: unknown command: $command" >&2
        echo "Usage: fish scripts/stt.fish <start|stop|restart|status|logs|android-pin|android-token|android-reverse> [args]" >&2
        exit 2
end
