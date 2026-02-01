#!/usr/bin/perl -w
#
# flamegraph-enhance-tooltips.pl - Enhance flame graph SVG with detailed tooltips
#
# Usage: ./flamegraph-enhance-tooltips.pl query.svg explain.json > query-enhanced.svg
#
# Adds detailed MySQL EXPLAIN information to flame graph tooltips, similar to
# Tanel Põder's SQL Plan FlameGraphs.
#
use strict;
use JSON::PP;

die "Usage: $0 <flamegraph.svg> <explain.json>\n" unless @ARGV == 2;

my ($svg_file, $json_file) = @ARGV;

# Read and parse JSON
open my $jfh, '<', $json_file or die "Cannot open $json_file: $!\n";
my $json_text = do { local $/; <$jfh> };
close $jfh;
$json_text =~ s/^.*?EXPLAIN:\s*//s;
my $data = decode_json($json_text);

# Build a lookup table of operation details
my %op_details;

sub process_node {
    my ($node, $depth) = @_;
    return unless ref $node eq 'HASH';

    my $op = $node->{operation} // 'unknown';
    
    # Build a simplified key for matching
    my $key = build_match_key($node);
    
    # Collect all available details
    my %details = (
        operation      => $op,
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
        used_columns   => $node->{used_columns} // [],
        covering       => $node->{covering},
        schema_name    => $node->{schema_name} // '',
    );
    
    $op_details{$key} = \%details;
    
    # Process children
    if (exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY') {
        for my $child (@{$node->{inputs}}) {
            process_node($child, $depth + 1);
        }
    }
}

sub build_match_key {
    my ($node) = @_;
    my $op = $node->{operation} // 'unknown';
    my $table = $node->{table_name} // '';
    my $index = $node->{index_name} // '';
    my $rows = $node->{actual_rows} // 0;
    my $loops = $node->{actual_loops} // 1;
    
    # Create a normalized key that can be matched against SVG title
    # Remove backticks and clean up
    $op =~ s/`//g;
    
    return lc("$op|$table|$index|$rows|$loops");
}

sub find_operation {
    my ($title_text) = @_;
    
    # Try to extract operation info from the title
    # Title format: "OPERATION starts=X rows=Y (TIME unit, PERCENT%)"
    
    my $best_match = undef;
    my $best_score = 0;
    
    for my $key (keys %op_details) {
        my $details = $op_details{$key};
        my $op = $details->{operation};
        my $table = $details->{table_name};
        my $index = $details->{index_name};
        my $rows = $details->{actual_rows} // 0;
        my $loops = $details->{actual_loops} // 1;
        
        my $score = 0;
        
        # High-value matches (index name is very specific)
        if ($index && $title_text =~ /\.\Q$index\E\]|\[\Q$index\E\]|using\s+\Q$index\E/i) {
            $score += 10;
        }
        
        # Table name match
        if ($table && $title_text =~ /\Q$table\E/i) {
            $score += 3;
        }
        
        # Rows must match exactly
        if ($title_text =~ /rows[=:]?\s*(\d+)\b/i) {
            my $title_rows = $1;
            if (int($rows + 0.5) == $title_rows) {
                $score += 5;
            }
        }
        
        # Loops/starts match
        if ($title_text =~ /starts[=:]?\s*(\d+)\b/i) {
            my $title_loops = $1;
            if ($loops == $title_loops) {
                $score += 3;
            }
        }
        
        # Operation type keywords (add score for matching type)
        if ($title_text =~ /TABLE.?SCAN/i && $op =~ /Table scan/i) {
            $score += 4;
        }
        if ($title_text =~ /INDEX.?RANGE.?SCAN/i && $op =~ /Index range scan/i) {
            $score += 4;
        }
        if ($title_text =~ /INDEX.?LOOKUP/i && $op =~ /Index lookup/i) {
            $score += 4;
        }
        if ($title_text =~ /\bFILTER\b/i && $op =~ /^Filter/i) {
            $score += 4;
        }
        if ($title_text =~ /\bSORT\b/i && $op =~ /Sort/i) {
            $score += 4;
        }
        if ($title_text =~ /Nested loop/i && $op =~ /Nested loop/i) {
            $score += 4;
        }
        if ($title_text =~ /Intersect/i && $op =~ /Intersect/i) {
            $score += 4;
        }
        if ($title_text =~ /Aggregate/i && $op =~ /Aggregate/i) {
            $score += 4;
        }
        if ($title_text =~ /GROUP/i && $op =~ /Group/i) {
            $score += 4;
        }
        
        # Update best match if this is better
        if ($score > $best_score) {
            $best_score = $score;
            $best_match = $details;
        }
    }
    
    # Require minimum score to return a match
    return ($best_score >= 8) ? $best_match : undef;
}

sub format_tooltip {
    my ($original_title, $details) = @_;
    
    return $original_title unless $details;
    
    my @lines = ();
    
    # Line 1: Original title (operation + time)
    push @lines, $original_title;
    push @lines, "";  # blank line
    
    # Line 2: Table and Index
    if ($details->{table_name}) {
        my $table_info = "Table: " . ($details->{schema_name} ? "$details->{schema_name}." : "") . $details->{table_name};
        if ($details->{index_name}) {
            $table_info .= " (index: $details->{index_name})";
        }
        push @lines, $table_info;
    }
    
    # Line 3: Access type
    if ($details->{access_type}) {
        push @lines, "Access: $details->{access_type}";
    }
    
    # Line 4: Rows
    if (defined $details->{actual_rows}) {
        my $rows_info = sprintf("Rows: %.0f actual", $details->{actual_rows});
        if (defined $details->{estimated_rows}) {
            $rows_info .= sprintf(" (%.0f estimated)", $details->{estimated_rows});
            # Add accuracy indicator
            my $ratio = $details->{estimated_rows} > 0 
                ? $details->{actual_rows} / $details->{estimated_rows} 
                : 0;
            if ($ratio > 2) {
                $rows_info .= " [UNDERESTIMATE]";
            } elsif ($ratio < 0.5 && $ratio > 0) {
                $rows_info .= " [OVERESTIMATE]";
            }
        }
        push @lines, $rows_info;
    }
    
    # Line 5: Loops/Starts
    if ($details->{actual_loops} && $details->{actual_loops} > 1) {
        push @lines, "Loops: $details->{actual_loops}";
    }
    
    # Line 6: Timing details
    if (defined $details->{actual_last_row_ms}) {
        my $time_info = sprintf("Time: %.3f ms (last row)", $details->{actual_last_row_ms});
        if (defined $details->{actual_first_row_ms}) {
            $time_info .= sprintf(", %.3f ms (first row)", $details->{actual_first_row_ms});
        }
        push @lines, $time_info;
    }
    
    # Line 7: Cost
    if (defined $details->{estimated_total_cost}) {
        push @lines, sprintf("Cost: %.2f", $details->{estimated_total_cost});
    }
    
    # Line 8: Condition (truncated if long)
    if ($details->{condition}) {
        my $cond = $details->{condition};
        $cond = substr($cond, 0, 80) . "..." if length($cond) > 83;
        push @lines, "Condition: $cond";
    }
    
    # Line 9: Ranges (for index scans)
    if ($details->{ranges} && @{$details->{ranges}}) {
        my $ranges = join(", ", @{$details->{ranges}});
        $ranges = substr($ranges, 0, 60) . "..." if length($ranges) > 63;
        push @lines, "Ranges: $ranges";
    }
    
    # Line 10: Covering index
    if (defined $details->{covering}) {
        push @lines, "Covering: " . ($details->{covering} ? "Yes" : "No");
    }
    
    return join("&#10;", @lines);  # &#10; is newline in SVG title
}

# Process JSON to build lookup
process_node($data, 0);

# Read SVG and enhance titles
open my $sfh, '<', $svg_file or die "Cannot open $svg_file: $!\n";
my $svg = do { local $/; <$sfh> };
close $sfh;

# Find and replace <title>...</title> elements
$svg =~ s{<title>([^<]+)</title>}{
    my $original = $1;
    my $details = find_operation($original);
    my $enhanced = format_tooltip($original, $details);
    "<title>$enhanced</title>"
}ge;

print $svg;
