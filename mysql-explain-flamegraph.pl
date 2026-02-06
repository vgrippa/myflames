#!/usr/bin/perl -w
#
# mysql-explain-flamegraph.pl - Generate flame graph SVG from MySQL EXPLAIN ANALYZE JSON
#
# All-in-one script that handles:
# - Parsing MySQL EXPLAIN ANALYZE FORMAT=JSON output
# - Automatic ms/us unit detection and conversion
# - Generating folded stacks
# - Calling flamegraph.pl with correct parameters
# - Enhancing tooltips with detailed information
#
# Usage: ./mysql-explain-flamegraph.pl [options] explain.json > query.svg
#
use strict;
use JSON::PP;
use Getopt::Long;
use File::Basename;
use File::Spec;
use IPC::Open2;

# Default options
my $width = 1800;
my $height = 32;
my $colors = "hot";
my $title = "MySQL Query Plan";
my $enhance_tooltips = 1;
my $inverted = 0;
my $help = 0;

GetOptions(
    'width=i'    => \$width,
    'height=i'   => \$height,
    'colors=s'   => \$colors,
    'title=s'    => \$title,
    'enhance!'   => \$enhance_tooltips,
    'inverted'   => \$inverted,
    'help'       => \$help,
) or die "Error in command line arguments\n";

if ($help) {
    print <<'USAGE';
Usage: mysql-explain-flamegraph.pl [options] explain.json > query.svg

Options:
  --width N        SVG width (default: 1800)
  --height N       Frame height (default: 32)
  --colors SCHEME  Color scheme: hot, mem, io, etc. (default: hot)
  --title TEXT     Title text (default: "MySQL Query Plan")
  --inverted       Generate icicle graph (inverted flame graph)
  --enhance        Enable enhanced tooltips (default: on)
  --no-enhance     Disable enhanced tooltips
  --help           Show this help

Example:
  ./mysql-explain-flamegraph.pl explain.json > query.svg
  ./mysql-explain-flamegraph.pl --title "Slow Query" --colors mem explain.json > query.svg
  ./mysql-explain-flamegraph.pl --inverted explain.json > query-icicle.svg
USAGE
    exit 0;
}

# Find flamegraph.pl in the same directory as this script
my $script_dir = dirname(File::Spec->rel2abs($0));
my $flamegraph_pl = File::Spec->catfile($script_dir, "flamegraph.pl");
die "Cannot find flamegraph.pl in $script_dir\n" unless -x $flamegraph_pl;

# Read JSON input
my $json_text = do { local $/; <> };
$json_text =~ s/^.*?EXPLAIN:\s*//s;
my $data = decode_json($json_text);

# Store operation details for tooltip enhancement
my %op_details;
my @output_lines;

#------------------------------------------------------------------------------
# Process JSON tree and build folded stacks
#------------------------------------------------------------------------------

sub build_label {
    my ($node) = @_;
    
    my $op = $node->{operation} // 'unknown';
    my $table = $node->{table_name} // '';
    my $index = $node->{index_name} // '';
    my $rows = $node->{actual_rows};
    my $loops = $node->{actual_loops} // 1;
    
    # Clean up operation
    $op =~ s/`//g;
    
    # Build descriptive label based on operation type
    my $label;
    
    if ($op =~ /^Table scan/i) {
        $label = "TABLE SCAN [$table]";
    }
    elsif ($op =~ /^Index range scan/i) {
        $label = "INDEX RANGE SCAN [$table.$index]";
    }
    elsif ($op =~ /^Index scan/i) {
        $label = "INDEX SCAN [$table.$index]";
    }
    elsif ($op =~ /^Index lookup/i) {
        $label = "INDEX LOOKUP [$table.$index]";
    }
    elsif ($op =~ /^Single-row index lookup/i) {
        $label = "SINGLE ROW LOOKUP [$table.$index]";
    }
    elsif ($op =~ /^Covering index/i) {
        $label = "COVERING INDEX [$table.$index]";
    }
    elsif ($op =~ /^Filter/i) {
        my $cond = $node->{condition} // '';
        $cond =~ s/`//g;
        $cond = substr($cond, 0, 50) . ".." if length($cond) > 52;
        $label = "FILTER ($cond)";
    }
    elsif ($op =~ /^Sort/i) {
        $label = "SORT";
        if ($op =~ /row IDs/i) { $label .= " (row IDs)"; }
        elsif ($op =~ /filesort/i) { $label .= " (filesort)"; }
    }
    elsif ($op =~ /^Nested loop/i) {
        if ($op =~ /inner/i) { $label = "NESTED LOOP INNER"; }
        elsif ($op =~ /left/i) { $label = "NESTED LOOP LEFT"; }
        elsif ($op =~ /semi/i) { $label = "NESTED LOOP SEMI"; }
        elsif ($op =~ /anti/i) { $label = "NESTED LOOP ANTI"; }
        else { $label = "NESTED LOOP"; }
    }
    elsif ($op =~ /^Aggregate/i) {
        $label = "AGGREGATE";
    }
    elsif ($op =~ /^Group/i) {
        $label = "GROUP";
    }
    elsif ($op =~ /^Materialize/i) {
        $label = "MATERIALIZE";
    }
    elsif ($op =~ /^Stream results/i) {
        $label = "STREAM";
    }
    elsif ($op =~ /^Limit/i) {
        $label = "LIMIT";
    }
    elsif ($op =~ /^Intersect/i) {
        $label = "Intersect rows sorted by row ID";
    }
    elsif ($op =~ /^Union/i) {
        $label = "UNION";
    }
    else {
        $label = $op;
        $label = substr($label, 0, 60) . ".." if length($label) > 62;
    }
    
    # Append metrics (Tanel Põder style)
    my @metrics;
    push @metrics, "starts=$loops" if defined $loops;
    push @metrics, sprintf("rows=%.0f", $rows) if defined $rows;
    $label .= " " . join(" ", @metrics) if @metrics;
    
    # Clean semicolons (they're the delimiter in folded format)
    $label =~ s/;/_/g;
    
    return $label;
}

sub process_node {
    my ($node, $path_ref) = @_;
    return unless ref $node eq 'HASH';
    
    my @path = @$path_ref;
    my $label = build_label($node);
    push @path, $label;
    
    my $loops = $node->{actual_loops} // 1;
    my $time_ms = ($node->{actual_last_row_ms} // 0) * $loops;
    
    # Store details for tooltip enhancement
    store_details($label, $node);
    
    # Process children
    my @children;
    if (exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY') {
        @children = @{$node->{inputs}};
    }
    
    # Sort children by time (largest first for better visualization)
    @children = sort { 
        (($b->{actual_last_row_ms} // 0) * ($b->{actual_loops} // 1)) <=>
        (($a->{actual_last_row_ms} // 0) * ($a->{actual_loops} // 1))
    } @children;
    
    # Calculate children time for self-time
    my $children_time = 0;
    foreach my $child (@children) {
        my $child_loops = $child->{actual_loops} // 1;
        my $child_time = ($child->{actual_last_row_ms} // 0) * $child_loops;
        $children_time += $child_time;
        process_node($child, \@path);
    }
    
    # Self-time
    my $self_time = $time_ms - $children_time;
    $self_time = 0 if $self_time < 0;
    
    push @output_lines, {
        path => [@path],
        time => $self_time,
    };
}

sub store_details {
    my ($label, $node) = @_;
    
    $op_details{$label} = {
        operation      => $node->{operation} // '',
        table_name     => $node->{table_name} // '',
        index_name     => $node->{index_name} // '',
        access_type    => $node->{access_type} // '',
        actual_rows    => $node->{actual_rows},
        actual_loops   => $node->{actual_loops} // 1,
        estimated_rows => $node->{estimated_rows},
        actual_last_row_ms   => $node->{actual_last_row_ms},
        actual_first_row_ms  => $node->{actual_first_row_ms},
        estimated_total_cost => $node->{estimated_total_cost},
        condition      => $node->{condition} // '',
        ranges         => $node->{ranges} // [],
        covering       => $node->{covering},
        schema_name    => $node->{schema_name} // '',
    };
}

#------------------------------------------------------------------------------
# Tooltip enhancement
#------------------------------------------------------------------------------

sub xml_escape {
    my ($str) = @_;
    return '' unless defined $str;
    $str =~ s/&/&amp;/g;
    $str =~ s/</&lt;/g;
    $str =~ s/>/&gt;/g;
    $str =~ s/"/&quot;/g;
    return $str;
}

sub enhance_tooltip {
    my ($original_title) = @_;
    
    # Find matching operation details
    my $details = find_matching_details($original_title);
    return $original_title unless $details;
    
    my @lines = ($original_title, "");
    
    # Table and Index
    if ($details->{table_name}) {
        my $table_info = "Table: " . 
            ($details->{schema_name} ? xml_escape($details->{schema_name}) . "." : "") . 
            xml_escape($details->{table_name});
        $table_info .= " (index: " . xml_escape($details->{index_name}) . ")" if $details->{index_name};
        push @lines, $table_info;
    }
    
    # Access type
    push @lines, "Access: $details->{access_type}" if $details->{access_type};
    
    # Rows
    if (defined $details->{actual_rows}) {
        my $rows_info = sprintf("Rows: %.0f actual", $details->{actual_rows});
        if (defined $details->{estimated_rows}) {
            $rows_info .= sprintf(" (%.0f estimated)", $details->{estimated_rows});
            my $ratio = $details->{estimated_rows} > 0 
                ? $details->{actual_rows} / $details->{estimated_rows} : 0;
            $rows_info .= " [UNDERESTIMATE]" if $ratio > 2;
            $rows_info .= " [OVERESTIMATE]" if $ratio < 0.5 && $ratio > 0;
        }
        push @lines, $rows_info;
    }
    
    # Loops
    push @lines, "Loops: $details->{actual_loops}" 
        if $details->{actual_loops} && $details->{actual_loops} > 1;
    
    # Timing
    if (defined $details->{actual_last_row_ms}) {
        my $time_info = sprintf("Time: %.3f ms (last row)", $details->{actual_last_row_ms});
        $time_info .= sprintf(", %.3f ms (first row)", $details->{actual_first_row_ms})
            if defined $details->{actual_first_row_ms};
        push @lines, $time_info;
    }
    
    # Cost
    push @lines, sprintf("Cost: %.2f", $details->{estimated_total_cost})
        if defined $details->{estimated_total_cost};
    
    # Condition
    if ($details->{condition}) {
        my $cond = xml_escape($details->{condition});
        $cond = substr($cond, 0, 80) . "..." if length($cond) > 83;
        push @lines, "Condition: $cond";
    }
    
    # Ranges
    if ($details->{ranges} && @{$details->{ranges}}) {
        my $ranges = xml_escape(join(", ", @{$details->{ranges}}));
        $ranges = substr($ranges, 0, 60) . "..." if length($ranges) > 63;
        push @lines, "Ranges: $ranges";
    }
    
    # Covering index
    push @lines, "Covering: " . ($details->{covering} ? "Yes" : "No")
        if defined $details->{covering};
    
    return join("&#10;", @lines);
}

sub find_matching_details {
    my ($title_text) = @_;
    
    my $best_match = undef;
    my $best_score = 0;
    
    for my $label (keys %op_details) {
        my $details = $op_details{$label};
        my $score = 0;
        
        # Check if the label is contained in the title
        if ($title_text =~ /\Q$label\E/i) {
            $score += 20;  # Exact label match is best
        }
        
        # Check index name (very specific)
        if ($details->{index_name} && 
            $title_text =~ /\.\Q$details->{index_name}\E\]|using\s+\Q$details->{index_name}\E/i) {
            $score += 10;
        }
        
        # Check table name
        if ($details->{table_name} && $title_text =~ /\Q$details->{table_name}\E/i) {
            $score += 3;
        }
        
        # Check rows
        if (defined $details->{actual_rows} && $title_text =~ /rows[=:]?\s*(\d+)\b/i) {
            $score += 5 if int($details->{actual_rows} + 0.5) == $1;
        }
        
        # Check loops
        if ($title_text =~ /starts[=:]?\s*(\d+)\b/i) {
            $score += 3 if ($details->{actual_loops} // 1) == $1;
        }
        
        $best_match = $details if $score > $best_score;
        $best_score = $score if $score > $best_score;
    }
    
    return ($best_score >= 5) ? $best_match : undef;
}

#------------------------------------------------------------------------------
# Main processing
#------------------------------------------------------------------------------

# Process JSON tree
process_node($data, []);

# Determine time unit (ms or us)
my $max_time = 0;
foreach my $entry (@output_lines) {
    $max_time = $entry->{time} if $entry->{time} > $max_time;
}

my $use_microseconds = ($max_time > 0 && $max_time < 1);
my $unit = $use_microseconds ? "us" : "ms";
my $multiplier = $use_microseconds ? 1000 : 1;

# Build folded stack output
my $folded_output = "";
foreach my $entry (@output_lines) {
    my $time = $entry->{time} * $multiplier;
    $time = int($time + 0.5);
    $time = 1 if $time == 0 && @{$entry->{path}} == 1;  # Ensure root appears
    next if $time <= 0;
    $folded_output .= join(";", @{$entry->{path}}) . " $time\n";
}

# Generate flame graph using IPC::Open2 for bidirectional communication
my @fg_cmd = (
    $flamegraph_pl,
    "--width", $width,
    "--height", $height,
    "--colors", $colors,
    "--title", $title,
    "--countname", $unit
);
push @fg_cmd, "--inverted" if $inverted;

my $pid = open2(my $fg_out, my $fg_in, @fg_cmd)
    or die "Cannot run flamegraph.pl: $!\n";

# Send folded data
print $fg_in $folded_output;
close $fg_in;

# Read SVG output
my $svg = do { local $/; <$fg_out> };
close $fg_out;
waitpid($pid, 0);

# Enhance tooltips if requested
if ($enhance_tooltips) {
    $svg =~ s{<title>([^<]+)</title>}{
        my $original = $1;
        my $enhanced = enhance_tooltip($original);
        "<title>$enhanced</title>"
    }ge;
}

# Output final SVG
print $svg;
