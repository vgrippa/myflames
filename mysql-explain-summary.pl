#!/usr/bin/perl -w
#
# mysql-explain-summary.pl - Show query operations sorted by time (slowest first)
#
use strict;
use JSON::PP;

my $json_text = do { local $/; <> };
$json_text =~ s/^.*?EXPLAIN:\s*//s;
my $data = decode_json($json_text);

my @operations = ();

sub process_node {
    my ($node, $depth) = @_;
    return unless ref $node eq 'HASH';
    
    my $op = $node->{operation} // 'unknown';
    my $table = $node->{table_name} // '';
    my $index = $node->{index_name} // '';
    my $rows = $node->{actual_rows} // 0;
    my $loops = $node->{actual_loops} // 1;
    my $time_ms = ($node->{actual_last_row_ms} // 0) * $loops;
    
    # Calculate children time
    my $children_time = 0;
    my @children = ();
    if (exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY') {
        @children = @{$node->{inputs}};
    }
    
    foreach my $child (@children) {
        next unless ref $child eq 'HASH';
        my $child_time = ($child->{actual_last_row_ms} // 0) * ($child->{actual_loops} // 1);
        $children_time += $child_time;
        process_node($child, $depth + 1);
    }
    
    my $self_time = $time_ms - $children_time;
    $self_time = 0 if $self_time < 0;
    
    $op =~ s/`//g;
    push @operations, {
        operation => $op,
        table => $table,
        index => $index,
        self_time => $self_time,
        total_time => $time_ms,
        rows => $rows,
        loops => $loops,
        depth => $depth,
    };
}

process_node($data, 0);

# Calculate total (sum of all self times)
my $total_time = 0;
foreach my $op (@operations) {
    $total_time += $op->{self_time};
}
$total_time = 1 if $total_time == 0;

# Sort by self_time descending
@operations = sort { $b->{self_time} <=> $a->{self_time} } @operations;

# Print header
printf "\n%-60s %10s %6s %12s %10s\n", "OPERATION", "SELF TIME", "%", "ROWS", "LOOPS";
print "-" x 100 . "\n";

# Print operations
foreach my $op (@operations) {
    my $pct = ($op->{self_time} / $total_time) * 100;
    my $name = substr($op->{operation}, 0, 58);
    
    # Add table.index info
    if ($op->{table} && $op->{index}) {
        $name .= " [$op->{table}.$op->{index}]" if length($name) < 40;
    } elsif ($op->{table}) {
        $name .= " [$op->{table}]" if length($name) < 45;
    }
    $name = substr($name, 0, 60);
    
    my $bar = "#" x int($pct / 2);
    
    printf "%-60s %8.0f ms %5.1f%% %12.0f %10d\n", 
        $name, $op->{self_time}, $pct, $op->{rows}, $op->{loops};
    printf "%-60s %s\n", "", $bar if $pct > 1;
}

print "\n";
printf "TOTAL: %.0f ms\n\n", $total_time;
